"""Requirement Refiner V2 dùng source tool-loop chỉ đọc và validation fail-closed."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Literal

from ...core.errors import HarnessError
from ...llm.client import LlmClient, LlmCompletion
from ...loop import RoleToolResultV1, canonical_json_hash
from ...sources.fixtures import UseCaseSource
from ...storage.runtime import RuntimeStore, utc_now_iso
from .model_v2 import RequirementModelV2, SCHEMA_VERSION
from .delta_v1 import (
    DELTA_SCHEMA_VERSION,
    MAX_DELTA_OPERATIONS,
    apply_requirement_delta,
    requirement_candidate_hash,
)
from .gap_report import build_requirement_gap_report, validate_requirement_gap_report
from .source_tools import RequirementSourceCatalog, observation_text_contains_quote
from .session_v1 import (
    SESSION_FILENAME,
    build_requirement_session_authority,
    complete_requirement_call,
    open_requirement_session,
    pending_requirement_call,
    persist_requirement_action_intent,
    read_requirement_action_intent,
    requirement_session_summary,
    reserve_requirement_call,
    set_requirement_session_status,
    write_requirement_session,
)
from .validator_v2 import (
    RequirementModelV2Issue,
    RequirementModelV2ValidationReport,
    normalize_and_validate_requirement_model_v2,
)


_PROMPT_PATH = Path(__file__).with_name("prompts") / "requirement_model_v2.md"
_DELTA_PROMPT_PATH = Path(__file__).with_name("prompts") / "requirement_delta_v1.md"
_ACTION_NAMES = {
    "source.list", "source.search", "source.read", "complete", "requirement.propose_delta",
}
_EXPECTED_ROOT_KEYS = {
    "use_case",
    "scenario_contract",
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
    "invalid_schema_version", "invalid_revision", "required_field", "invalid_type",
    "invalid_requirement_status", "invalid_question_status", "invalid_decision_status",
    "required_id", "invalid_id_prefix", "duplicate_id", "dangling_reference",
    "question_dependency_cycle", "evidence_not_in_source", "explicit_without_evidence",
    "explicit_with_invalid_evidence", "human_decision_without_accepted_decision",
    "unknown_without_question", "unknown_contains_inferred_content",
    "resolved_question_without_decision", "exception_in_main_flow",
    "exception_in_postcondition", "invalid_scenario_contract_version",
    "scenario_contract_empty", "invalid_scenario_kind", "invalid_surface_set",
    "invalid_surface_status", "surface_reason_required", "scenario_requirement_required",
    "scenario_acceptance_required", "scenario_evidence_required",
    "scenario_question_coverage_missing", "scenario_question_unrelated",
    "surface_requirement_required", "surface_not_applicable_conflict",
    "surface_clarification_question_required", "surface_question_not_open",
    "surface_question_unrelated", "scenario_question_not_mapped", "scenario_coverage_missing",
    "invalid_provenance_schema_version", "provenance_sources_empty", "invalid_source_type",
    "invalid_source_revision", "source_root_mismatch", "source_snapshot_missing",
    "source_fingerprint_mismatch", "question_without_evidence",
    "source_conflict_evidence_insufficient",
    "branch_behavior_without_clarification", "acceptance_without_verifies",
    "acceptance_placeholder",
}
_DELTA_UNSAFE_ISSUE_CODES = {
    "required_id", "invalid_id_prefix", "duplicate_id", "invalid_type", "invalid_schema_version",
    "invalid_revision", "invalid_scenario_contract_version", "scenario_contract_empty",
    "invalid_surface_set", "invalid_provenance_schema_version", "provenance_sources_empty",
    "invalid_source_type", "invalid_source_revision", "source_root_mismatch",
    "source_snapshot_missing", "source_fingerprint_mismatch",
}


@dataclass(frozen=True)
class RequirementModelV2Attempt:
    """Audit metadata cho một lần Agent nộp candidate model."""

    attempt: int
    purpose: Literal["initial", "format_recovery", "validator_repair"]
    finish_reason: str
    usage: dict[str, object]
    json_mode_requested: bool
    max_tokens: int
    parse_status: Literal["parsed", "parse_failed", "schema_failed", "truncated", "delta_failed"]
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
    """Canonical model cùng audit của source loop và bounded candidate repair."""

    model: RequirementModelV2
    initial_report: RequirementModelV2ValidationReport | None
    final_report: RequirementModelV2ValidationReport
    repaired: bool
    attempts: list[RequirementModelV2Attempt]
    source_manifest_path: str = ""
    source_journal_path: str = ""
    gap_report: dict[str, object] | None = None
    gap_report_path: str = ""
    delta_artifact_paths: list[str] | None = None
    session_path: str = ""

    def to_report_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "repaired": self.repaired,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "initial_report": self.initial_report.to_dict() if self.initial_report else None,
            "final_report": self.final_report.to_dict(),
        }
        if self.source_manifest_path:
            payload["source_manifest_path"] = self.source_manifest_path
        if self.source_journal_path:
            payload["source_journal_path"] = self.source_journal_path
        if self.gap_report_path:
            payload["gap_report_path"] = self.gap_report_path
        if self.delta_artifact_paths:
            payload["delta_artifact_paths"] = list(self.delta_artifact_paths)
        return payload


class RequirementModelV2Refiner:
    """Tạo canonical Requirement Model V2 qua vòng source tools có giới hạn."""

    def __init__(
        self,
        llm_client: LlmClient,
        *,
        store: RuntimeStore | None = None,
        max_actions: int | None = None,
        max_action_retries: int | None = None,
    ):
        self.llm_client = llm_client
        self.store = store
        settings = getattr(llm_client, "settings", None)
        self.max_actions = max_actions or int(getattr(settings, "requirement_max_tool_actions", 12))
        configured_retries = int(getattr(settings, "requirement_max_action_retries", 2))
        self.max_action_retries = configured_retries if max_action_retries is None else max_action_retries

    def refine(
        self,
        source: UseCaseSource,
        *,
        run_id: str,
        source_artifact_ref: str,
        source_catalog: RequirementSourceCatalog | None = None,
        resume: bool = False,
    ) -> RequirementModelV2RefinementResult:
        if not run_id.strip():
            raise HarnessError("requirement_model_v2_validation_failed", "run_id must not be empty.")
        if not source_artifact_ref.strip():
            raise HarnessError(
                "requirement_model_v2_validation_failed",
                "source_artifact_ref must not be empty.",
                run_id=run_id,
            )
        if resume and (self.store is None or not self.store.artifact_path(run_id, SESSION_FILENAME).exists()):
            raise HarnessError(
                "requirement_session_not_resumable",
                "Historical Requirement run has no durable loop session.",
                run_id=run_id,
            )
        catalog = source_catalog or self._fallback_catalog(
            source=source,
            run_id=run_id,
            source_artifact_ref=source_artifact_ref,
        )
        if catalog.run_id != run_id:
            raise HarnessError(
                "requirement_source_catalog_invalid",
                "Requirement source catalog belongs to a different run.",
                run_id=run_id,
            )
        primary = self._primary_source(catalog, source.source_id, source_artifact_ref, run_id=run_id)
        manifest_path, journal_path, journal = self._start_audit(
            run_id=run_id,
            source=source,
            catalog=catalog,
            resume=resume,
        )
        max_tokens = self._structured_max_tokens()
        authority = build_requirement_session_authority(
            manifest_input_hash=str(journal.get("manifest_input_hash") or ""),
            source_fingerprints=[str(item.get("fingerprint") or "") for item in catalog.sources],
            requirement_contract_version=SCHEMA_VERSION,
            prompt_policy_sha256=self._prompt_policy_sha256(),
            llm_mode=str(getattr(getattr(self.llm_client, "settings", None), "llm_mode", "unknown")),
            llm_model=str(getattr(getattr(self.llm_client, "settings", None), "llm_model", "unknown")),
        )
        session, session_path = open_requirement_session(
            store=self.store,
            run_id=run_id,
            authority=authority,
            action_limit=self.max_actions,
            action_retry_limit=self.max_action_retries,
            resume=resume,
        )
        if resume:
            journal["status"] = "running"
            journal["last_error"] = None
            journal["updated_at"] = utc_now_iso()
            self._write_journal(journal_path, journal)
        try:
            context = self._restore_context(
                run_id=run_id,
                catalog=catalog,
                journal=journal,
                session=session,
            )
        except HarnessError as exc:
            self._finish_journal_failure(journal, journal_path, exc)
            set_requirement_session_status(
                store=self.store,
                run_id=run_id,
                session=session,
                status="blocked",
                error=exc,
            )
            raise

        try:
            recovered = self._reconcile_pending_call(
                run_id=run_id,
                source=source,
                primary=primary,
                catalog=catalog,
                journal=journal,
                journal_path=journal_path,
                session=session,
                session_path=session_path,
                context=context,
                max_tokens=max_tokens,
            )
            if recovered is not None:
                return recovered
            while int(session["budgets"]["action_remaining"]) > 0:
                call_id = reserve_requirement_call(
                    store=self.store,
                    run_id=run_id,
                    session=session,
                )
                action_index = int(call_id.split("-", 1)[1])
                prompt = self._action_prompt(
                    source=source,
                    source_count=len(catalog.sources),
                    history=context["history"],
                    action_index=action_index,
                    repair_base=context["repair_base"],
                    repair_report=context["repair_report"],
                )
                started = time.monotonic()
                try:
                    completion = self._complete(prompt, max_tokens=max_tokens)
                except HarnessError as exc:
                    if exc.error_code == "llm_transport_failed":
                        set_requirement_session_status(
                            store=self.store,
                            run_id=run_id,
                            session=session,
                            status="paused",
                            error=HarnessError(exc.error_code, exc.message, run_id=run_id),
                        )
                        journal["status"] = "paused"
                        journal["last_error"] = exc.to_response()
                        journal["updated_at"] = utc_now_iso()
                        self._write_journal(journal_path, journal)
                    raise
                duration = round(time.monotonic() - started, 3)
                completion_metadata = {
                    "finish_reason": completion.finish_reason,
                    "usage": deepcopy(completion.usage),
                    "duration_seconds": duration,
                    "max_tokens": max_tokens,
                }
                if completion.finish_reason.strip().lower() == "length":
                    budgets = session["budgets"]
                    budgets["truncated_failures"] = int(budgets["truncated_failures"]) + 1
                    write_requirement_session(store=self.store, run_id=run_id, session=session)
                    result = {
                        "ok": False,
                        "error_code": "requirement_tool_action_truncated",
                        "message": "Return one smaller JSON action.",
                    }
                    context["history"].append({"action": "rejected", "result": result})
                    self._append_journal(
                        journal, journal_path, action_index=action_index, action="rejected",
                        arguments={}, summary="", result=result, completion=completion, duration=duration,
                        input_payload={"action": "rejected", "arguments": {}},
                    )
                    complete_requirement_call(store=self.store, run_id=run_id, session=session)
                    if int(budgets["truncated_failures"]) > self.max_action_retries:
                        raise HarnessError(
                            "requirement_tool_action_truncated",
                            "Requirement source action remained truncated after bounded retries.",
                            run_id=run_id,
                        )
                    continue
                try:
                    action = self._parse_action(completion.content)
                    session["budgets"]["invalid_failures"] = 0
                    session["budgets"]["truncated_failures"] = 0
                except HarnessError as exc:
                    budgets = session["budgets"]
                    budgets["invalid_failures"] = int(budgets["invalid_failures"]) + 1
                    write_requirement_session(store=self.store, run_id=run_id, session=session)
                    result = {"ok": False, "error_code": exc.error_code, "message": exc.message}
                    context["history"].append({"action": "rejected", "result": result})
                    self._append_journal(
                        journal, journal_path, action_index=action_index, action="rejected",
                        arguments={}, summary="", result=result, completion=completion, duration=duration,
                        input_payload={"action": "rejected", "arguments": {}},
                    )
                    complete_requirement_call(store=self.store, run_id=run_id, session=session)
                    if int(budgets["invalid_failures"]) > self.max_action_retries:
                        raise HarnessError(
                            "requirement_tool_action_invalid",
                            "Requirement Refiner repeatedly returned an invalid action.",
                            run_id=run_id,
                        ) from exc
                    continue

                persist_requirement_action_intent(
                    store=self.store,
                    run_id=run_id,
                    session=session,
                    action=action,
                    completion_metadata=completion_metadata,
                    reserve_candidate=self._action_reserves_candidate(action, context=context),
                )
                result = self._execute_requirement_action(
                    run_id=run_id,
                    source=source,
                    primary=primary,
                    catalog=catalog,
                    manifest_path=manifest_path,
                    journal=journal,
                    journal_path=journal_path,
                    session=session,
                    session_path=session_path,
                    context=context,
                    action=action,
                    completion=completion,
                    duration=duration,
                    max_tokens=max_tokens,
                )
                if result is not None:
                    return result
            raise HarnessError(
                "requirement_tool_action_limit_exceeded",
                f"Requirement Refiner did not complete within {self.max_actions} actions.",
                run_id=run_id,
            )
        except Exception as exc:
            if not (isinstance(exc, HarnessError) and exc.error_code == "llm_transport_failed"):
                self._finish_journal_failure(journal, journal_path, exc)
                if session.get("status") != "completed":
                    error = exc if isinstance(exc, HarnessError) else HarnessError("internal_error", str(exc), run_id=run_id)
                    set_requirement_session_status(
                        store=self.store,
                        run_id=run_id,
                        session=session,
                        status="blocked",
                        error=error,
                    )
            error = exc if isinstance(exc, HarnessError) else HarnessError("internal_error", str(exc), run_id=run_id)
            gap_report = build_requirement_gap_report(
                run_id=run_id,
                attempts=[attempt.to_dict() for attempt in context["attempts"]],
                error=error,
                source_manifest_input_hash=str(journal.get("manifest_input_hash") or "") or None,
            )
            self._write_gap_report(run_id, gap_report)
            raise

    def _execute_requirement_action(
        self,
        *,
        run_id: str,
        source: UseCaseSource,
        primary: dict[str, object],
        catalog: RequirementSourceCatalog,
        manifest_path: str,
        journal: dict[str, object],
        journal_path: str,
        session: dict[str, Any],
        session_path: str,
        context: dict[str, Any],
        action: dict[str, object],
        completion: LlmCompletion,
        duration: float,
        max_tokens: int,
    ) -> RequirementModelV2RefinementResult | None:
        pending = pending_requirement_call(session, run_id=run_id)
        action_index = int(pending["action_index"])
        name = str(action["action"])
        arguments = dict(action["arguments"])
        summary = str(action.get("summary") or "")
        observations = context["observations"]
        terminal_status: str | None = None

        if name == "source.list":
            result = catalog.list_sources() if not arguments else {
                "ok": False,
                "error_code": "requirement_source_arguments_invalid",
                "message": "source.list does not accept arguments.",
            }
            context["listed"] = context["listed"] or result.get("ok") is True
        elif name == "source.search":
            result = catalog.search(
                query=arguments.get("query"),
                source_ids=arguments.get("source_ids"),
                max_results=arguments.get("max_results", 10),
            )
            if result.get("ok") is True:
                for match in self._dict_items(result.get("matches")):
                    observation_id = f"OBS-{len(observations) + 1}"
                    match["observation_id"] = observation_id
                    observations[observation_id] = deepcopy(match)
        elif name == "source.read":
            result = catalog.read(
                source_id=arguments.get("source_id"),
                start_line=arguments.get("start_line", 1),
                line_count=arguments.get("line_count", 200),
            )
            if result.get("ok") is True:
                observation_id = f"OBS-{len(observations) + 1}"
                result["observation_id"] = observation_id
                observations[observation_id] = deepcopy(result)
        elif name == "requirement.propose_delta":
            result, final = self._execute_delta_action(
                run_id=run_id,
                catalog=catalog,
                manifest_path=manifest_path,
                journal=journal,
                journal_path=journal_path,
                session=session,
                session_path=session_path,
                context=context,
                action_index=action_index,
                arguments=arguments,
                summary=summary,
                completion=completion,
                duration=duration,
                max_tokens=max_tokens,
            )
            return final
        else:
            result, final = self._execute_complete_action(
                run_id=run_id,
                source=source,
                primary=primary,
                catalog=catalog,
                manifest_path=manifest_path,
                journal=journal,
                journal_path=journal_path,
                session=session,
                session_path=session_path,
                context=context,
                action_index=action_index,
                arguments=arguments,
                summary=summary,
                completion=completion,
                duration=duration,
                max_tokens=max_tokens,
            )
            return final

        history_arguments = arguments
        context["history"].append(
            {"action": name, "arguments": history_arguments, "summary": summary, "result": deepcopy(result)}
        )
        self._append_journal(
            journal,
            journal_path,
            action_index=action_index,
            action=name,
            arguments=self._journal_arguments(name, arguments),
            summary=summary,
            result=result,
            completion=completion,
            duration=duration,
            status=terminal_status,
            input_payload={"action": name, "arguments": arguments},
        )
        self._persist_working_context(run_id=run_id, session=session, context=context)
        complete_requirement_call(store=self.store, run_id=run_id, session=session)
        return None

    def _execute_complete_action(
        self,
        *,
        run_id: str,
        source: UseCaseSource,
        primary: dict[str, object],
        catalog: RequirementSourceCatalog,
        manifest_path: str,
        journal: dict[str, object],
        journal_path: str,
        session: dict[str, Any],
        session_path: str,
        context: dict[str, Any],
        action_index: int,
        arguments: dict[str, object],
        summary: str,
        completion: LlmCompletion,
        duration: float,
        max_tokens: int,
    ) -> tuple[dict[str, object], RequirementModelV2RefinementResult | None]:
        if not context["listed"] or not context["observations"]:
            result = {
                "ok": False,
                "error_code": "requirement_source_observation_required",
                "message": "Call source.list and inspect source content before complete.",
            }
            self._finish_non_terminal_action(
                run_id=run_id, journal=journal, journal_path=journal_path, session=session,
                context=context, action_index=action_index, name="complete", arguments=arguments,
                summary=summary, result=result, completion=completion, duration=duration,
            )
            return result, None
        if context["repair_base"] is not None:
            result = {
                "ok": False,
                "error_code": "requirement_delta_required",
                "message": "A parsed invalid candidate must be repaired with requirement.propose_delta.",
            }
            self._finish_non_terminal_action(
                run_id=run_id, journal=journal, journal_path=journal_path, session=session,
                context=context, action_index=action_index, name="complete", arguments=arguments,
                summary=summary, result=result, completion=completion, duration=duration,
            )
            return result, None

        candidate_count = int(session["budgets"]["candidate_used"])
        raw_model = arguments.get("requirement_model")
        processed = self._process_candidate(
            raw_model=raw_model,
            completion=completion,
            attempt_number=candidate_count,
            source=source,
            primary_source=primary,
            catalog=catalog,
            observations=context["observations"],
            run_id=run_id,
            max_tokens=max_tokens,
        )
        context["attempts"].append(processed["attempt"])
        report = processed["report"]
        model = processed["model"]
        if context["initial_report"] is None and isinstance(report, RequirementModelV2ValidationReport):
            context["initial_report"] = report
        if isinstance(report, RequirementModelV2ValidationReport) and report.ok and isinstance(model, RequirementModelV2):
            result = {"ok": True, "status": "completed", "candidate_attempt": candidate_count}
            return result, self._complete_refinement(
                run_id=run_id,
                model=model,
                report=report,
                repaired=candidate_count > 1,
                manifest_path=manifest_path,
                journal=journal,
                journal_path=journal_path,
                session=session,
                session_path=session_path,
                context=context,
                action_index=action_index,
                name="complete",
                arguments=arguments,
                summary=summary,
                result=result,
                completion=completion,
                duration=duration,
            )

        can_use_delta = self._can_use_delta_base(processed)
        result = {
            "ok": False,
            "error_code": "requirement_model_v2_validation_failed",
            "candidate_attempt": candidate_count,
            "validation_report": processed["report_dict"],
            "instruction": self._repair_instruction(processed, use_delta=can_use_delta),
        }
        if candidate_count >= 2:
            self._persist_working_context(run_id=run_id, session=session, context=context)
            self._append_journal(
                journal, journal_path, action_index=action_index, action="complete",
                arguments=self._journal_arguments("complete", arguments), summary=summary,
                result=result, completion=completion, duration=duration, status="failed",
                input_payload={"action": "complete", "arguments": arguments},
            )
            complete_requirement_call(store=self.store, run_id=run_id, session=session)
            self._raise_candidate_error(processed, run_id=run_id)
        self._require_repairable(processed, run_id=run_id)
        if can_use_delta and isinstance(model, RequirementModelV2):
            context["repair_base"] = model
            context["repair_report"] = deepcopy(processed["report_dict"])
            context["repair_base_ref"] = self._write_internal_model(
                run_id=run_id,
                filename=f"requirement_working_candidate_v2_a{candidate_count}.json",
                model=model,
            )
        self._finish_non_terminal_action(
            run_id=run_id, journal=journal, journal_path=journal_path, session=session,
            context=context, action_index=action_index, name="complete", arguments=arguments,
            summary=summary, result=result, completion=completion, duration=duration,
        )
        return result, None

    def _execute_delta_action(
        self,
        *,
        run_id: str,
        catalog: RequirementSourceCatalog,
        manifest_path: str,
        journal: dict[str, object],
        journal_path: str,
        session: dict[str, Any],
        session_path: str,
        context: dict[str, Any],
        action_index: int,
        arguments: dict[str, object],
        summary: str,
        completion: LlmCompletion,
        duration: float,
        max_tokens: int,
    ) -> tuple[dict[str, object], RequirementModelV2RefinementResult | None]:
        repair_base = context["repair_base"]
        if not context["listed"] or not context["observations"]:
            result = {
                "ok": False,
                "error_code": "requirement_source_observation_required",
                "message": "Call source.list and inspect source content before proposing a delta.",
            }
            self._finish_non_terminal_action(
                run_id=run_id, journal=journal, journal_path=journal_path, session=session,
                context=context, action_index=action_index, name="requirement.propose_delta",
                arguments=arguments, summary=summary, result=result, completion=completion,
                duration=duration,
            )
            return result, None
        if not isinstance(repair_base, RequirementModelV2):
            result = {
                "ok": False,
                "error_code": "requirement_delta_not_ready",
                "message": "A parsed invalid candidate is required before proposing a delta.",
            }
            self._finish_non_terminal_action(
                run_id=run_id, journal=journal, journal_path=journal_path, session=session,
                context=context, action_index=action_index, name="requirement.propose_delta",
                arguments=arguments, summary=summary, result=result, completion=completion,
                duration=duration,
            )
            return result, None

        candidate_count = int(session["budgets"]["candidate_used"])
        raw_delta = arguments.get("requirement_delta")
        try:
            application = apply_requirement_delta(
                base_model=repair_base,
                raw_delta=raw_delta,
                observations=context["observations"],
                source_text_by_id=catalog.source_text_by_id,
            )
        except HarnessError as exc:
            report_dict = self._schema_report(exc.message, code=exc.error_code)
            context["attempts"].append(
                self._attempt(
                    candidate_count, "validator_repair", completion, max_tokens,
                    parse_status="delta_failed", validation_report=report_dict,
                )
            )
            delta_path = self._write_delta_artifact(
                run_id=run_id, attempt=1, raw_delta=raw_delta, status="rejected",
                base_hash=requirement_candidate_hash(repair_base), result_hash=None,
                validation_report=report_dict, error=exc,
            )
            if delta_path:
                context["delta_artifact_paths"].append(delta_path)
            result = {
                "ok": False, "error_code": exc.error_code, "message": exc.message,
                "candidate_attempt": candidate_count, "validation_report": report_dict,
            }
            self._persist_working_context(run_id=run_id, session=session, context=context)
            self._append_journal(
                journal, journal_path, action_index=action_index,
                action="requirement.propose_delta",
                arguments=self._journal_arguments("requirement.propose_delta", arguments),
                summary=summary, result=result, completion=completion, duration=duration,
                status="failed",
                input_payload={"action": "requirement.propose_delta", "arguments": arguments},
            )
            complete_requirement_call(store=self.store, run_id=run_id, session=session)
            raise HarnessError(exc.error_code, exc.message, run_id=run_id) from exc

        context["attempts"].append(
            self._attempt(
                candidate_count, "validator_repair", completion, max_tokens,
                parse_status="parsed", validation_report=application.report.to_dict(),
            )
        )
        delta_path = self._write_delta_artifact(
            run_id=run_id, attempt=1,
            raw_delta={
                "schema_version": DELTA_SCHEMA_VERSION,
                "base_candidate_sha256": application.base_candidate_sha256,
                "operations": application.operations,
            },
            status="applied", base_hash=application.base_candidate_sha256,
            result_hash=application.result_candidate_sha256,
            validation_report=application.report.to_dict(), error=None,
        )
        if delta_path:
            context["delta_artifact_paths"].append(delta_path)
        result = {
            "ok": True,
            "status": "completed",
            "candidate_attempt": candidate_count,
            "base_candidate_sha256": application.base_candidate_sha256,
            "result_candidate_sha256": application.result_candidate_sha256,
        }
        return result, self._complete_refinement(
            run_id=run_id, model=application.model, report=application.report, repaired=True,
            manifest_path=manifest_path, journal=journal, journal_path=journal_path,
            session=session, session_path=session_path, context=context,
            action_index=action_index, name="requirement.propose_delta", arguments=arguments,
            summary=summary, result=result, completion=completion, duration=duration,
        )

    def _finish_non_terminal_action(
        self,
        *,
        run_id: str,
        journal: dict[str, object],
        journal_path: str,
        session: dict[str, Any],
        context: dict[str, Any],
        action_index: int,
        name: str,
        arguments: dict[str, object],
        summary: str,
        result: dict[str, object],
        completion: LlmCompletion,
        duration: float,
    ) -> None:
        context["history"].append(
            {
                "action": name,
                "arguments": self._journal_arguments(name, arguments),
                "summary": summary,
                "result": self._journal_result(name, result),
            }
        )
        self._persist_working_context(run_id=run_id, session=session, context=context)
        self._append_journal(
            journal, journal_path, action_index=action_index, action=name,
            arguments=self._journal_arguments(name, arguments), summary=summary,
            result=result, completion=completion, duration=duration,
            input_payload={"action": name, "arguments": arguments},
        )
        complete_requirement_call(store=self.store, run_id=run_id, session=session)

    def _complete_refinement(
        self,
        *,
        run_id: str,
        model: RequirementModelV2,
        report: RequirementModelV2ValidationReport,
        repaired: bool,
        manifest_path: str,
        journal: dict[str, object],
        journal_path: str,
        session: dict[str, Any],
        session_path: str,
        context: dict[str, Any],
        action_index: int,
        name: str,
        arguments: dict[str, object],
        summary: str,
        result: dict[str, object],
        completion: LlmCompletion,
        duration: float,
    ) -> RequirementModelV2RefinementResult:
        final_ref = self._write_internal_model(
            run_id=run_id,
            filename="requirement_loop_final_model_v1.json",
            model=model,
        )
        context["final_model_ref"] = final_ref
        context["final_report"] = report
        context["repaired"] = repaired
        self._persist_working_context(run_id=run_id, session=session, context=context)
        self._append_journal(
            journal, journal_path, action_index=action_index, action=name,
            arguments=self._journal_arguments(name, arguments), summary=summary,
            result=result, completion=completion, duration=duration, status="completed",
            input_payload={"action": name, "arguments": arguments},
        )
        complete_requirement_call(store=self.store, run_id=run_id, session=session)
        set_requirement_session_status(
            store=self.store, run_id=run_id, session=session, status="completed",
        )
        gap_report = build_requirement_gap_report(
            run_id=run_id,
            attempts=[attempt.to_dict() for attempt in context["attempts"]],
            model=model,
            source_manifest_input_hash=str(journal.get("manifest_input_hash") or "") or None,
        )
        gap_report_path = self._write_gap_report(run_id, gap_report)
        return RequirementModelV2RefinementResult(
            model=model,
            initial_report=context["initial_report"],
            final_report=report,
            repaired=repaired,
            attempts=context["attempts"],
            source_manifest_path=manifest_path,
            source_journal_path=journal_path,
            gap_report=gap_report,
            gap_report_path=gap_report_path,
            delta_artifact_paths=context["delta_artifact_paths"],
            session_path=session_path,
        )

    def _restore_context(
        self,
        *,
        run_id: str,
        catalog: RequirementSourceCatalog,
        journal: dict[str, object],
        session: dict[str, Any],
    ) -> dict[str, Any]:
        working = session["working"]
        attempts = [self._attempt_from_dict(item, run_id=run_id) for item in working.get("attempts", [])]
        initial_report = self._report_from_dict(working.get("initial_report"), run_id=run_id)
        final_report = self._report_from_dict(working.get("final_report"), run_id=run_id)
        repair_base_ref = str(working.get("repair_base_ref") or "")
        context: dict[str, Any] = {
            "history": [],
            "observations": {},
            "attempts": attempts,
            "initial_report": initial_report,
            "listed": False,
            "repair_base": self._read_internal_model(run_id, repair_base_ref) if repair_base_ref else None,
            "repair_base_ref": repair_base_ref,
            "repair_report": deepcopy(working.get("repair_report")),
            "delta_artifact_paths": [str(item) for item in working.get("delta_artifact_paths", [])],
            "final_model_ref": str(working.get("final_model_ref") or ""),
            "final_report": final_report,
            "repaired": bool(working.get("repaired", False)),
        }
        if isinstance(context["repair_base"], RequirementModelV2) and (
            working.get("repair_base_sha256") != requirement_candidate_hash(context["repair_base"])
        ):
            raise HarnessError(
                "requirement_session_invalid",
                "Requirement repair base hash does not match the durable session.",
                run_id=run_id,
            )
        if context["final_model_ref"]:
            final_model = self._read_internal_model(run_id, context["final_model_ref"])
            if working.get("final_model_sha256") != requirement_candidate_hash(final_model):
                raise HarnessError(
                    "requirement_session_invalid",
                    "Requirement final model hash does not match the durable session.",
                    run_id=run_id,
                )
        calls = journal.get("calls")
        if not isinstance(calls, list):
            raise HarnessError("requirement_source_audit_invalid", "Requirement source journal calls are invalid.", run_id=run_id)
        seen_call_ids: set[str] = set()
        for call in calls:
            if not isinstance(call, dict):
                raise HarnessError("requirement_source_audit_invalid", "Requirement source journal call is invalid.", run_id=run_id)
            action = str(call.get("action") or "")
            call_id = str(call.get("call_id") or "")
            if not call_id or call_id in seen_call_ids:
                raise HarnessError(
                    "requirement_source_audit_invalid",
                    "Requirement source journal contains a duplicate or empty call ID.",
                    run_id=run_id,
                )
            seen_call_ids.add(call_id)
            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
            replayed: dict[str, object] | None = None
            if action in {"source.list", "source.search", "source.read"}:
                replayed = self._replay_source_action(
                    action=action,
                    arguments=arguments,
                    catalog=catalog,
                    observations=context["observations"],
                )
                envelope = call.get("tool_result")
                if envelope is not None:
                    tool_result = RoleToolResultV1.from_dict(envelope)
                    if (
                        tool_result.call_id != call_id
                        or tool_result.input_sha256
                        != canonical_json_hash({"action": action, "arguments": arguments})
                        or tool_result.output_sha256 != canonical_json_hash(replayed)
                    ):
                        raise HarnessError(
                            "requirement_session_replay_mismatch",
                            f"Frozen source replay changed for {tool_result.call_id}.",
                            run_id=run_id,
                        )
                if action == "source.list" and replayed.get("ok") is True:
                    context["listed"] = True
            context["history"].append(
                {
                    "action": action,
                    "arguments": deepcopy(arguments),
                    "summary": str(call.get("summary") or ""),
                    "result": (
                        deepcopy(replayed)
                        if replayed is not None
                        else deepcopy(call.get("result")) if isinstance(call.get("result"), dict) else {}
                    ),
                }
            )
        stored_observation_count = int(working.get("observation_count") or 0)
        pending = session["cursor"].get("pending_call")
        pending_call_id = str(pending.get("call_id") or "") if isinstance(pending, dict) else ""
        pending_has_result = bool(
            pending_call_id
            and any(isinstance(item, dict) and item.get("call_id") == pending_call_id for item in calls)
        )
        if stored_observation_count != len(context["observations"]) and not (
            pending_has_result and stored_observation_count <= len(context["observations"])
        ):
            raise HarnessError(
                "requirement_session_replay_mismatch",
                "Restored Requirement observation count does not match the durable session.",
                run_id=run_id,
            )
        return context

    def _reconcile_pending_call(
        self,
        *,
        run_id: str,
        source: UseCaseSource,
        primary: dict[str, object],
        catalog: RequirementSourceCatalog,
        journal: dict[str, object],
        journal_path: str,
        session: dict[str, Any],
        session_path: str,
        context: dict[str, Any],
        max_tokens: int,
    ) -> RequirementModelV2RefinementResult | None:
        pending_value = session["cursor"].get("pending_call")
        if pending_value is None:
            return None
        pending = pending_requirement_call(session, run_id=run_id)
        call_id = str(pending["call_id"])
        calls = journal.get("calls") if isinstance(journal.get("calls"), list) else []
        if any(isinstance(item, dict) and item.get("call_id") == call_id for item in calls):
            self._persist_working_context(run_id=run_id, session=session, context=context)
            complete_requirement_call(store=self.store, run_id=run_id, session=session)
            if context["final_model_ref"] and isinstance(context["final_report"], RequirementModelV2ValidationReport):
                model = self._read_internal_model(run_id, context["final_model_ref"])
                set_requirement_session_status(
                    store=self.store, run_id=run_id, session=session, status="completed",
                )
                return self._result_from_context(
                    run_id=run_id, model=model, manifest_path=self._manifest_ref(run_id),
                    journal_path=journal_path, session_path=session_path, context=context,
                    journal=journal,
                )
            return None
        if pending.get("status") == "action_persisted":
            if self.store is None:
                raise HarnessError("requirement_session_not_resumable", "Requirement resume requires RuntimeStore.", run_id=run_id)
            action, metadata = read_requirement_action_intent(store=self.store, run_id=run_id, session=session)
            completion = LlmCompletion(
                content="",
                finish_reason=str(metadata.get("finish_reason") or "recovered"),
                usage=deepcopy(metadata.get("usage")) if isinstance(metadata.get("usage"), dict) else {},
            )
            return self._execute_requirement_action(
                run_id=run_id, source=source, primary=primary, catalog=catalog,
                manifest_path=self._manifest_ref(run_id), journal=journal, journal_path=journal_path,
                session=session, session_path=session_path, context=context, action=action,
                completion=completion, duration=float(metadata.get("duration_seconds") or 0.0),
                max_tokens=int(metadata.get("max_tokens") or max_tokens),
            )

        result = {
            "ok": False,
            "error_code": "requirement_llm_call_interrupted",
            "message": "The reserved LLM call ended before a structured action was persisted.",
        }
        action_index = int(pending["action_index"])
        completion = LlmCompletion(content="", finish_reason="interrupted", usage={})
        self._append_journal(
            journal, journal_path, action_index=action_index, action="interrupted",
            arguments={}, summary="", result=result, completion=completion, duration=0.0,
            input_payload={"action": "interrupted", "arguments": {}},
            result_status="interrupted",
        )
        context["history"].append({"action": "interrupted", "result": result})
        complete_requirement_call(store=self.store, run_id=run_id, session=session)
        return None

    def _replay_source_action(
        self,
        *,
        action: str,
        arguments: dict[str, object],
        catalog: RequirementSourceCatalog,
        observations: dict[str, dict[str, object]],
    ) -> dict[str, object]:
        if action == "source.list":
            return catalog.list_sources() if not arguments else {
                "ok": False,
                "error_code": "requirement_source_arguments_invalid",
                "message": "source.list does not accept arguments.",
            }
        if action == "source.search":
            result = catalog.search(
                query=arguments.get("query"), source_ids=arguments.get("source_ids"),
                max_results=arguments.get("max_results", 10),
            )
            if result.get("ok") is True:
                for match in self._dict_items(result.get("matches")):
                    observation_id = f"OBS-{len(observations) + 1}"
                    match["observation_id"] = observation_id
                    observations[observation_id] = deepcopy(match)
            return result
        result = catalog.read(
            source_id=arguments.get("source_id"), start_line=arguments.get("start_line", 1),
            line_count=arguments.get("line_count", 200),
        )
        if result.get("ok") is True:
            observation_id = f"OBS-{len(observations) + 1}"
            result["observation_id"] = observation_id
            observations[observation_id] = deepcopy(result)
        return result

    def _persist_working_context(
        self,
        *,
        run_id: str,
        session: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        working = session["working"]
        working.update(
            {
                "listed": bool(context["listed"]),
                "observation_count": len(context["observations"]),
                "attempts": [attempt.to_dict() for attempt in context["attempts"]],
                "initial_report": context["initial_report"].to_dict() if context["initial_report"] else None,
                "repair_base_ref": context.get("repair_base_ref") or None,
                "repair_base_sha256": (
                    requirement_candidate_hash(context["repair_base"])
                    if isinstance(context.get("repair_base"), RequirementModelV2)
                    else None
                ),
                "repair_report": deepcopy(context.get("repair_report")),
                "delta_artifact_paths": list(context["delta_artifact_paths"]),
                "final_model_ref": context.get("final_model_ref") or None,
                "final_model_sha256": (
                    requirement_candidate_hash(
                        self._read_internal_model(run_id, str(context.get("final_model_ref") or ""))
                    )
                    if context.get("final_model_ref")
                    else None
                ),
                "final_report": context["final_report"].to_dict() if context.get("final_report") else None,
                "repaired": bool(context.get("repaired", False)),
            }
        )
        write_requirement_session(store=self.store, run_id=run_id, session=session)

    def _result_from_context(
        self,
        *,
        run_id: str,
        model: RequirementModelV2,
        manifest_path: str,
        journal_path: str,
        session_path: str,
        context: dict[str, Any],
        journal: dict[str, object],
    ) -> RequirementModelV2RefinementResult:
        report = context["final_report"]
        gap_report = build_requirement_gap_report(
            run_id=run_id, attempts=[item.to_dict() for item in context["attempts"]],
            model=model, source_manifest_input_hash=str(journal.get("manifest_input_hash") or "") or None,
        )
        gap_report_path = self._write_gap_report(run_id, gap_report)
        return RequirementModelV2RefinementResult(
            model=model, initial_report=context["initial_report"], final_report=report,
            repaired=bool(context["repaired"]), attempts=context["attempts"],
            source_manifest_path=manifest_path, source_journal_path=journal_path,
            gap_report=gap_report, gap_report_path=gap_report_path,
            delta_artifact_paths=context["delta_artifact_paths"], session_path=session_path,
        )

    def _write_internal_model(self, *, run_id: str, filename: str, model: RequirementModelV2) -> str:
        if self.store is None:
            return ""
        path = self.store.write_json_artifact(run_id, filename, model.to_dict())
        return self.store.relative(path)

    def _read_internal_model(self, run_id: str, artifact_ref: str) -> RequirementModelV2:
        if self.store is None or not artifact_ref:
            raise HarnessError("requirement_session_invalid", "Requirement working model is unavailable.", run_id=run_id)
        path = (self.store.repo_root / artifact_ref).resolve()
        if path.parent != self.store.artifact_path(run_id, "placeholder.json").parent.resolve():
            raise HarnessError("requirement_session_invalid", "Requirement working model path is invalid.", run_id=run_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError("requirement_session_invalid", "Requirement working model is unreadable.", run_id=run_id) from exc
        return RequirementModelV2.from_dict(payload)

    def _manifest_ref(self, run_id: str) -> str:
        if self.store is None:
            return ""
        return self.store.relative(self.store.artifact_path(run_id, "requirement_source_manifest_v1.json"))

    @staticmethod
    def _attempt_from_dict(value: object, *, run_id: str) -> RequirementModelV2Attempt:
        if not isinstance(value, dict):
            raise HarnessError("requirement_session_invalid", "Requirement attempt is invalid.", run_id=run_id)
        try:
            return RequirementModelV2Attempt(
                attempt=int(value["attempt"]), purpose=value["purpose"],
                finish_reason=str(value.get("finish_reason") or ""),
                usage=deepcopy(value.get("usage")) if isinstance(value.get("usage"), dict) else {},
                json_mode_requested=value.get("json_mode_requested") is True,
                max_tokens=int(value.get("max_tokens") or 0), parse_status=value["parse_status"],
                validation_report=deepcopy(value.get("validation_report"))
                if isinstance(value.get("validation_report"), dict) else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HarnessError("requirement_session_invalid", "Requirement attempt is invalid.", run_id=run_id) from exc

    @staticmethod
    def _report_from_dict(value: object, *, run_id: str) -> RequirementModelV2ValidationReport | None:
        if value is None:
            return None
        if not isinstance(value, dict) or not isinstance(value.get("issues"), list):
            raise HarnessError("requirement_session_invalid", "Requirement validation report is invalid.", run_id=run_id)
        issues = []
        for item in value["issues"]:
            if not isinstance(item, dict):
                raise HarnessError("requirement_session_invalid", "Requirement validation issue is invalid.", run_id=run_id)
            issues.append(
                RequirementModelV2Issue(
                    code=str(item.get("code") or ""), path=str(item.get("path") or ""),
                    message=str(item.get("message") or ""),
                )
            )
        blocking = value.get("blocking_question_ids")
        if not isinstance(blocking, list):
            raise HarnessError("requirement_session_invalid", "Requirement validation report is invalid.", run_id=run_id)
        return RequirementModelV2ValidationReport(
            ok=value.get("ok") is True, status=str(value.get("status") or ""),
            issues=issues, blocking_question_ids=[str(item) for item in blocking],
        )

    def _prompt_policy_sha256(self) -> str:
        return canonical_json_hash(
            {
                "model_prompt": _PROMPT_PATH.read_text(encoding="utf-8"),
                "delta_prompt": _DELTA_PROMPT_PATH.read_text(encoding="utf-8"),
                "clarification_disposition_policy": "clarification_disposition_v3",
                "actions": sorted(_ACTION_NAMES),
                "repairable_issue_codes": sorted(_REPAIRABLE_ISSUE_CODES),
            }
        )

    @staticmethod
    def _action_reserves_candidate(
        action: dict[str, object], *, context: dict[str, Any]
    ) -> bool:
        name = str(action.get("action") or "")
        has_source = bool(context.get("listed")) and bool(context.get("observations"))
        if name == "complete":
            return has_source and context.get("repair_base") is None
        if name == "requirement.propose_delta":
            return has_source and isinstance(context.get("repair_base"), RequirementModelV2)
        return False

    def _process_candidate(
        self,
        *,
        raw_model: object,
        completion: LlmCompletion,
        attempt_number: int,
        source: UseCaseSource,
        primary_source: dict[str, object],
        catalog: RequirementSourceCatalog,
        observations: dict[str, dict[str, object]],
        run_id: str,
        max_tokens: int,
    ) -> dict[str, object]:
        purpose: Literal["initial", "validator_repair"] = "initial" if attempt_number == 1 else "validator_repair"
        if not isinstance(raw_model, dict) or not _EXPECTED_ROOT_KEYS.issubset(raw_model):
            report_dict = self._schema_report("Complete action must contain a full requirement_model object.")
            return {
                "model": None, "report": None, "report_dict": report_dict,
                "schema_error": report_dict["issues"][0]["message"],
                "attempt": self._attempt(
                    attempt_number, purpose, completion, max_tokens,
                    parse_status="schema_failed", validation_report=report_dict,
                ),
            }
        payload = deepcopy(raw_model)
        try:
            self._bind_evidence_to_observations(payload, observations, run_id=run_id)
            self._stamp_python_owned_fields(
                payload, source=source, primary_source=primary_source, catalog=catalog, run_id=run_id,
            )
            validation = normalize_and_validate_requirement_model_v2(payload, catalog.source_text_by_id)
        except HarnessError as exc:
            report_dict = self._schema_report(exc.message, code=exc.error_code)
            return {
                "model": None, "report": None, "report_dict": report_dict,
                "schema_error": exc.message,
                "attempt": self._attempt(
                    attempt_number, purpose, completion, max_tokens,
                    parse_status="schema_failed", validation_report=report_dict,
                ),
            }
        return {
            "model": validation.model,
            "report": validation.report,
            "report_dict": validation.report.to_dict(),
            "schema_error": "",
            "attempt": self._attempt(
                attempt_number, purpose, completion, max_tokens,
                parse_status="parsed", validation_report=validation.report.to_dict(),
            ),
        }

    @staticmethod
    def _bind_evidence_to_observations(
        payload: dict[str, Any],
        observations: dict[str, dict[str, object]],
        *,
        run_id: str,
    ) -> None:
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            raise HarnessError(
                "requirement_evidence_observation_invalid", "Candidate evidence must be a list.", run_id=run_id,
            )
        for index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise HarnessError(
                    "requirement_evidence_observation_invalid",
                    f"Evidence item {index} must be an object.", run_id=run_id,
                )
            observation_id = str(item.pop("observation_ref", "") or "")
            observation = observations.get(observation_id)
            if observation is None or not observation_text_contains_quote(observation, item.get("quote")):
                raise HarnessError(
                    "requirement_evidence_observation_invalid",
                    f"Evidence {item.get('id') or index} is not grounded in observation {observation_id!r}.",
                    run_id=run_id,
                )
            item.pop("source_ref", None)
            item["source_id"] = str(observation.get("source_id") or "")

    @staticmethod
    def _stamp_python_owned_fields(
        payload: dict[str, Any],
        *,
        source: UseCaseSource,
        primary_source: dict[str, object],
        catalog: RequirementSourceCatalog,
        run_id: str,
    ) -> None:
        payload["schema_version"] = SCHEMA_VERSION
        payload["revision"] = 1
        payload["run_id"] = run_id
        used_ids = {
            str(item.get("source_id") or "")
            for item in payload.get("evidence", [])
            if isinstance(item, dict)
        }
        used_ids.add(source.source_id)
        ledger_sources = [item for item in catalog.sources if str(item.get("id") or "") in used_ids]
        payload["source"] = {
            "source_id": primary_source["id"],
            "source_ref": primary_source["source_ref"],
            "artifact_ref": primary_source["artifact_ref"],
            "fingerprint": primary_source["fingerprint"],
        }
        payload["provenance_ledger"] = {
            "schema_version": "requirement_provenance_v1",
            "sources": ledger_sources,
        }
        payload["decisions"] = []
        payload["validation"] = {"status": "invalid", "errors": []}

    def _action_prompt(
        self,
        *,
        source: UseCaseSource,
        source_count: int,
        history: list[dict[str, object]],
        action_index: int,
        repair_base: RequirementModelV2 | None,
        repair_report: dict[str, object] | None,
    ) -> str:
        contract_path = _DELTA_PROMPT_PATH if repair_base is not None else _PROMPT_PATH
        model_contract = contract_path.read_text(encoding="utf-8")
        model_contract = self._replace_prompt_tokens(
            model_contract,
            {
                "DATASET": source.dataset,
                "USE_CASE": source.use_case,
                "SOURCE_REF": "python_owned_after_tool_observation",
                "SOURCE_TEXT": "Source text is not injected. Use source tools and cite OBS-* observations.",
            },
        )
        state = {
            "action_index": action_index,
            "max_actions": self.max_actions,
            "available_source_count": source_count,
            "history": history,
        }
        if repair_base is not None:
            state["repair_context"] = {
                "required_action": "requirement.propose_delta",
                "delta_schema_version": DELTA_SCHEMA_VERSION,
                "max_operations": MAX_DELTA_OPERATIONS,
                "base_candidate_sha256": requirement_candidate_hash(repair_base),
                "base_candidate": repair_base.to_dict(),
                "validation_report": deepcopy(repair_report),
            }
        repair_contract = (
            "A parsed invalid candidate exists in repair_context. You MUST call "
            "requirement.propose_delta with arguments.requirement_delta. Do not call complete. "
            "The delta root has schema_version, base_candidate_sha256, and operations. "
            "Operation shapes: entity.update={op,target_id,fields}; "
            "entity.add={op,collection,temp_ref,value}; entity.remove={op,target_id}; "
            "surface.update={op,scenario_id,surface,fields}. "
            "Add collections: use_case.actors, preconditions, flows.main, flows.alternatives, "
            "flows.exceptions, postconditions, business_rules, acceptance_criteria, "
            "quality_requirements, assumptions, open_questions, evidence, and "
            "scenario_contract.scenarios. Use temp_ref=tmp:<name> for added entities and exact "
            "temp refs inside reference fields. Never provide or mutate stable IDs, root metadata, "
            "provenance, decisions, validation, or effective_blocking. New or changed evidence "
            "quotes must cite observation_ref=OBS-*. When adding an open question, also place its "
            "temp_ref in every affected scenario.open_question_refs and in each related surface "
            "whose requirement_refs intersects the question.affects list.\n"
            if repair_base is not None
            else "Call complete with arguments.requirement_model after inspecting source content.\n"
        )
        return (
            "# Requirement Source Tool Loop\n\n"
            "Return exactly one JSON action object and no prose or markdown.\n"
            "Action schema: {\"action\":\"source.list|source.search|source.read|complete|requirement.propose_delta\","
            "\"arguments\":{},\"summary\":\"short reason\"}.\n"
            "Call source.list first. Inspect source content before complete. "
            "source.search arguments: query, optional source_ids, optional max_results. "
            "source.read arguments: source_id, optional start_line and line_count. "
            "complete arguments: requirement_model. Every evidence item in that candidate must use "
            "observation_ref=OBS-* from a successful read/search result; source identity is Python-owned.\n\n"
            + repair_contract
            + "\n"
            "TOOL_STATE_JSON_BEGIN\n"
            + json.dumps(state, ensure_ascii=False, indent=2)
            + "\nTOOL_STATE_JSON_END\n\n"
            + model_contract
        )

    def _complete(self, prompt: str, *, max_tokens: int) -> LlmCompletion:
        try:
            return self.llm_client.complete_with_metadata(
                system=(
                    "You are Requirement Refiner V2 in a controlled read-only source loop. "
                    "Return exactly one JSON action. Never invent source identity or human decisions."
                ),
                prompt=prompt,
                json_mode=True,
                max_tokens=max_tokens,
            )
        except HarnessError:
            raise
        except Exception as exc:  # pragma: no cover - defensive adapter boundary.
            raise HarnessError("requirement_refiner_failed", str(exc)) from exc

    @staticmethod
    def _parse_action(raw: str) -> dict[str, object]:
        try:
            payload = json.loads(raw.strip())
        except json.JSONDecodeError as exc:
            raise HarnessError("requirement_tool_action_invalid", "Requirement action is not valid JSON.") from exc
        if not isinstance(payload, dict):
            raise HarnessError("requirement_tool_action_invalid", "Requirement action must be a JSON object.")
        name = str(payload.get("action") or "").strip()
        arguments = payload.get("arguments")
        if name not in _ACTION_NAMES or not isinstance(arguments, dict):
            raise HarnessError(
                "requirement_tool_action_invalid",
                "Requirement action has an unsupported action or arguments shape.",
            )
        return {"action": name, "arguments": deepcopy(arguments), "summary": str(payload.get("summary") or "")}

    def _start_audit(
        self,
        *,
        run_id: str,
        source: UseCaseSource,
        catalog: RequirementSourceCatalog,
        resume: bool,
    ) -> tuple[str, str, dict[str, object]]:
        if self.store is None:
            return "", "", {
                "version": "requirement_source_journal_v1", "run_id": run_id,
                "status": "running", "calls": [],
            }
        manifest = {
            "version": "requirement_source_manifest_v1",
            "run_id": run_id,
            "primary_source_id": source.source_id,
            "allowed_actions": sorted(_ACTION_NAMES),
            "max_actions": self.max_actions,
            "max_action_retries": self.max_action_retries,
            "sources": catalog.sources,
        }
        manifest["input_hash"] = self._json_hash(manifest)
        manifest_file = self.store.artifact_path(run_id, "requirement_source_manifest_v1.json")
        if manifest_file.exists():
            try:
                existing = json.loads(manifest_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HarnessError(
                    "requirement_source_audit_invalid",
                    "Existing Requirement source manifest is unreadable.", run_id=run_id,
                ) from exc
            if not isinstance(existing, dict) or existing.get("input_hash") != manifest["input_hash"]:
                raise HarnessError(
                    "requirement_source_input_drift",
                    "Requirement source catalog or tool policy changed for the active run.", run_id=run_id,
                )
        else:
            self.store.write_json_artifact(run_id, "requirement_source_manifest_v1.json", manifest)
        journal_file = self.store.artifact_path(run_id, "requirement_source_journal_v1.json")
        if resume:
            try:
                journal = json.loads(journal_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise HarnessError(
                    "requirement_session_not_resumable",
                    "Requirement source journal is unavailable for resume.",
                    run_id=run_id,
                ) from exc
            if (
                not isinstance(journal, dict)
                or journal.get("version") != "requirement_source_journal_v1"
                or journal.get("run_id") != run_id
                or journal.get("manifest_input_hash") != manifest["input_hash"]
                or not isinstance(journal.get("calls"), list)
            ):
                raise HarnessError(
                    "requirement_source_audit_invalid",
                    "Requirement source journal identity is invalid.",
                    run_id=run_id,
                )
            return self.store.relative(manifest_file), self.store.relative(journal_file), journal
        journal = {
            "version": "requirement_source_journal_v1",
            "run_id": run_id,
            "manifest_input_hash": manifest["input_hash"],
            "status": "running",
            "calls": [],
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        journal_file = self.store.write_json_artifact(run_id, "requirement_source_journal_v1.json", journal)
        return self.store.relative(manifest_file), self.store.relative(journal_file), journal

    def _append_journal(
        self,
        journal: dict[str, object],
        journal_path: str,
        *,
        action_index: int,
        action: str,
        arguments: dict[str, object],
        summary: str,
        result: dict[str, object],
        completion: LlmCompletion,
        duration: float,
        status: str | None = None,
        input_payload: object | None = None,
        result_status: Literal["succeeded", "rejected", "failed", "interrupted"] | None = None,
    ) -> None:
        call_id = f"RST-{action_index}"
        envelope_status = result_status or (
            "succeeded" if result.get("ok") is True else "failed" if status == "failed" else "rejected"
        )
        error = None
        if envelope_status in {"failed", "interrupted"}:
            error = {
                "error_code": str(result.get("error_code") or "requirement_action_failed"),
                "message": str(result.get("message") or "Requirement action did not complete."),
            }
        evidence_refs: list[str] = []
        if action == "source.search":
            evidence_refs = [
                str(item.get("observation_id") or "") for item in self._dict_items(result.get("matches"))
            ]
        elif action == "source.read" and result.get("observation_id"):
            evidence_refs = [str(result["observation_id"])]
        tool_result = RoleToolResultV1.build(
            call_id=call_id,
            tool_id=action,
            status=envelope_status,
            retryable=envelope_status in {"rejected", "interrupted"},
            input_payload=input_payload if input_payload is not None else {"action": action, "arguments": arguments},
            output_payload=result,
            evidence_refs=evidence_refs,
            error=error,
        )
        calls = journal.setdefault("calls", [])
        if isinstance(calls, list):
            calls.append(
                {
                    "call_id": call_id, "action": action,
                    "arguments": deepcopy(arguments), "summary": summary,
                    "result": self._journal_result(action, result),
                    "tool_result": tool_result.to_dict(),
                    "finish_reason": completion.finish_reason,
                    "usage": deepcopy(completion.usage), "duration_seconds": duration,
                    "recorded_at": utc_now_iso(),
                }
            )
        if status:
            journal["status"] = status
        journal["updated_at"] = utc_now_iso()
        self._write_journal(journal_path, journal)

    def _finish_journal_failure(
        self,
        journal: dict[str, object],
        journal_path: str,
        exc: Exception,
    ) -> None:
        journal["status"] = "failed"
        journal["last_error"] = {
            "error_code": exc.error_code if isinstance(exc, HarnessError) else "internal_error",
            "message": exc.message if isinstance(exc, HarnessError) else str(exc),
        }
        journal["updated_at"] = utc_now_iso()
        self._write_journal(journal_path, journal)

    def _write_journal(self, journal_path: str, journal: dict[str, object]) -> None:
        if self.store is None or not journal_path:
            return
        self.store.write_json_artifact(str(journal.get("run_id") or ""), Path(journal_path).name, journal)

    def _write_gap_report(self, run_id: str, report: dict[str, object]) -> str:
        if self.store is None:
            return ""
        validate_requirement_gap_report(report, run_id=run_id)
        path = self.store.write_json_artifact(run_id, "requirement_gap_report_v1.json", report)
        return self.store.relative(path)

    def _write_delta_artifact(
        self,
        *,
        run_id: str,
        attempt: int,
        raw_delta: object,
        status: str,
        base_hash: str,
        result_hash: str | None,
        validation_report: dict[str, object],
        error: HarnessError | None,
    ) -> str:
        if self.store is None:
            return ""
        payload: dict[str, object] = {
            "schema_version": DELTA_SCHEMA_VERSION,
            "run_id": run_id,
            "attempt": attempt,
            "status": status,
            "base_candidate_sha256": base_hash,
            "result_candidate_sha256": result_hash,
            "delta": deepcopy(raw_delta),
            "validation_report": deepcopy(validation_report),
            "error": error.to_response() if error is not None else None,
        }
        path = self.store.write_json_artifact(
            run_id,
            f"requirement_delta_v1_a{attempt}.json",
            payload,
        )
        return self.store.relative(path)

    @staticmethod
    def _journal_arguments(action: str, arguments: dict[str, object]) -> dict[str, object]:
        if action == "requirement.propose_delta":
            delta = arguments.get("requirement_delta")
            base_hash = delta.get("base_candidate_sha256") if isinstance(delta, dict) else None
            return {
                "base_candidate_sha256": base_hash,
                "delta_sha256": RequirementModelV2Refiner._json_hash(delta),
            }
        if action != "complete":
            return deepcopy(arguments)
        return {"candidate_sha256": RequirementModelV2Refiner._json_hash(arguments.get("requirement_model"))}

    @staticmethod
    def _journal_result(action: str, result: dict[str, object]) -> dict[str, object]:
        """Giữ audit hữu ích nhưng không sao chép nội dung source vào journal."""

        common = {
            key: deepcopy(result[key])
            for key in ("ok", "error_code", "message", "status", "candidate_attempt", "instruction")
            if key in result
        }
        if action == "source.list":
            sources = RequirementModelV2Refiner._dict_items(result.get("sources"))
            common["source_count"] = len(sources)
            common["source_ids"] = [str(item.get("source_id") or "") for item in sources]
        elif action == "source.search":
            matches = RequirementModelV2Refiner._dict_items(result.get("matches"))
            common.update(
                {
                    "match_count": len(matches),
                    "observation_ids": [str(item.get("observation_id") or "") for item in matches],
                    "truncated": bool(result.get("truncated", False)),
                }
            )
        elif action == "source.read":
            for key in (
                "source_id", "fingerprint", "line_start", "line_end", "next_start_line",
                "end_of_source", "truncated_by_limit", "observation_id",
            ):
                if key in result:
                    common[key] = deepcopy(result[key])
        elif action in {"complete", "requirement.propose_delta"} and "validation_report" in result:
            common["validation_report"] = deepcopy(result["validation_report"])
        if action == "requirement.propose_delta":
            for key in ("base_candidate_sha256", "result_candidate_sha256"):
                if key in result:
                    common[key] = result[key]
        return common

    @staticmethod
    def _fallback_catalog(
        *, source: UseCaseSource, run_id: str, source_artifact_ref: str,
    ) -> RequirementSourceCatalog:
        fingerprint = "sha256:" + hashlib.sha256(source.text.encode("utf-8")).hexdigest()
        entry: dict[str, object] = {
            "id": source.source_id, "type": source.source_type,
            "source_ref": source.source_ref, "locator": source.locator,
            "revision": source.revision, "artifact_ref": source_artifact_ref,
            "fingerprint": fingerprint,
        }
        return RequirementSourceCatalog(
            run_id=run_id, sources=[entry], source_text_by_id={source.source_id: source.text},
        )

    @staticmethod
    def _primary_source(
        catalog: RequirementSourceCatalog,
        source_id: str,
        source_artifact_ref: str,
        *,
        run_id: str,
    ) -> dict[str, object]:
        for item in catalog.sources:
            if item.get("id") == source_id and item.get("artifact_ref") == source_artifact_ref:
                return item
        raise HarnessError(
            "requirement_source_catalog_invalid",
            "Primary source is missing from the verified source catalog.", run_id=run_id,
        )

    @staticmethod
    def _attempt(
        attempt_number: int,
        purpose: Literal["initial", "format_recovery", "validator_repair"],
        completion: LlmCompletion,
        max_tokens: int,
        *,
        parse_status: Literal["parsed", "parse_failed", "schema_failed", "truncated", "delta_failed"],
        validation_report: dict[str, object] | None = None,
    ) -> RequirementModelV2Attempt:
        return RequirementModelV2Attempt(
            attempt=attempt_number, purpose=purpose, finish_reason=completion.finish_reason,
            usage=deepcopy(completion.usage), json_mode_requested=True, max_tokens=max_tokens,
            parse_status=parse_status, validation_report=validation_report,
        )

    def _structured_max_tokens(self) -> int:
        settings = getattr(self.llm_client, "settings", None)
        return int(getattr(settings, "llm_structured_max_tokens", 8192))

    @staticmethod
    def _require_repairable(processed: dict[str, object], *, run_id: str) -> None:
        report = processed.get("report")
        if not isinstance(report, RequirementModelV2ValidationReport):
            return
        non_repairable = sorted({issue.code for issue in report.issues if issue.code not in _REPAIRABLE_ISSUE_CODES})
        if non_repairable:
            raise HarnessError(
                "requirement_model_v2_validation_failed",
                "Requirement Model V2 contains non-repairable semantic violations: " + ", ".join(non_repairable),
                run_id=run_id,
            )

    @staticmethod
    def _can_use_delta_base(processed: dict[str, object]) -> bool:
        model = processed.get("model")
        report = processed.get("report")
        return (
            isinstance(model, RequirementModelV2)
            and isinstance(report, RequirementModelV2ValidationReport)
            and not {issue.code for issue in report.issues}.intersection(_DELTA_UNSAFE_ISSUE_CODES)
        )

    @staticmethod
    def _repair_instruction(processed: dict[str, object], *, use_delta: bool) -> str:
        """Chuyển validator finding thành chỉ dẫn sửa ngắn, không thay đổi ownership."""

        report = processed.get("report_dict")
        issues = report.get("issues") if isinstance(report, dict) else []
        normalized = [item for item in issues if isinstance(item, dict)] if isinstance(issues, list) else []
        codes = {str(item.get("code") or "") for item in normalized}
        paths = [str(item.get("path") or "") for item in normalized]
        guidance = [
            (
                "Repair only the reported violations with requirement.propose_delta, preserve valid content, "
                "and bind the exact base_candidate_sha256."
                if use_delta
                else "No safe delta base exists; return one corrected full model with complete."
            )
        ]
        if any(
            item.get("code") == "dangling_reference"
            and ".scenarios[" in str(item.get("path") or "")
            and str(item.get("path") or "").endswith(".requirement_refs")
            and "AC-" in str(item.get("message") or "")
            for item in normalized
        ):
            guidance.append(
                "Move every AC-* ID out of scenario.requirement_refs and into scenario.acceptance_refs; "
                "scenario.requirement_refs accepts only MF-*, AF-*, EF-*, and BR-* IDs."
            )
        if "scenario_question_coverage_missing" in codes:
            guidance.append(
                "Add every question named by the finding to that scenario.open_question_refs."
            )
        if "surface_question_unrelated" in codes:
            guidance.append(
                "Remove the named question from that surface and map it only to a surface whose "
                "requirement_refs intersects the question.affects list."
            )
        if "scenario_coverage_missing" in codes:
            guidance.append(
                "Map each uncovered ID named by the finding to a relevant scenario: AC-* IDs go to "
                "scenario.acceptance_refs; MF-*, AF-*, EF-*, and BR-* IDs go to "
                "scenario.requirement_refs."
            )
        if "unknown_without_question" in codes:
            guidance.append(
                "If the repair adds an open question, also add its temp_ref to every affected "
                "scenario.open_question_refs and to each related surface whose requirement_refs "
                "intersects the question.affects list."
            )
        if any(".surfaces." in path and path.endswith(".requirement_refs") for path in paths):
            guidance.append(
                "For a scenario-local surface reference, first add the related behavior to the parent "
                "scenario.requirement_refs or acceptance to scenario.acceptance_refs; remove it only if unrelated."
            )
        return " ".join(guidance)

    @staticmethod
    def _raise_candidate_error(processed: dict[str, object], *, run_id: str) -> None:
        report = processed.get("report")
        if isinstance(report, RequirementModelV2ValidationReport):
            details = "; ".join(f"{issue.code}@{issue.path}" for issue in report.issues)
        else:
            details = str(processed.get("schema_error") or "invalid structured output")
        raise HarnessError(
            "requirement_model_v2_validation_failed",
            "Requirement Model V2 failed validation after the bounded repair attempt: " + details,
            run_id=run_id,
        )

    @staticmethod
    def _schema_report(message: str, *, code: str = "requirement_model_v2_invalid") -> dict[str, object]:
        return {
            "ok": False, "status": "invalid",
            "issues": [{"code": code, "path": "$", "message": message}],
            "blocking_question_ids": [],
        }

    @staticmethod
    def _replace_prompt_tokens(template: str, values: dict[str, str]) -> str:
        output = template
        for key, value in values.items():
            output = output.replace("{{" + key + "}}", value)
        return output

    @staticmethod
    def _dict_items(value: object) -> list[dict[str, Any]]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @staticmethod
    def _json_hash(value: object) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()
