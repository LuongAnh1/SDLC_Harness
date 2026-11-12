"""Validator code trace cho Phase 4.

Module này chạy sau khi Developer.act/repair đã ghi code và Verification Runner
đã pass. Validator giữ hai lớp kiểm soát: rule-based evidence để chắc chắn
file/test tồn tại, sau đó bắt buộc gọi LLM để review graph ngữ nghĩa giữa
requirement, approved design, code và test. Kết quả được chuẩn hóa thành
missing link repairable hoặc non-repairable để workflow biết nên giao lại
Developer sửa code/test hay block/reopen checkpoint nền.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Callable

from ...core.config import phase4_llm_call_options
from ...core.errors import HarnessError
from ...llm.client import LlmClient
from ...llm.json_utils import parse_first_json_object
from ...llm.step_review import LlmStepReviewRequired
from ...profiles import (
    CodeTracePolicy,
    PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
    UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
)
from ...storage.runtime import utc_now_iso
from .code_contract import (
    build_finding_ledger,
    build_validation_claims,
    deterministic_typescript_contract_findings,
    snapshot_fingerprint,
)
from .code_review_tasks import (
    CODE_VALIDATION_MODE,
    CodeValidationTaskCache,
    build_code_validation_review_plan,
)
from .typescript_ast import TypeScriptAstEvidence, TypeScriptAstExtractor

MAX_SNIPPET_TOTAL_CHARS = 60000
MAX_SNIPPET_FILE_CHARS = 20000
MAX_TASK_SOURCE_SNIPPET_TOTAL_CHARS = 60000
MAX_TASK_SOURCE_SNIPPET_FILE_CHARS = 26000
MAX_TASK_TEST_SNIPPET_TOTAL_CHARS = 50000
MAX_TASK_TEST_SNIPPET_FILE_CHARS = 24000
TASK_SYMBOL_CONTEXT_LINES = 6
NON_REPAIRABLE_LLM_LINK_TYPES = {"trace_mapping_issue", "evidence_incomplete"}
NON_BLOCKING_MESSAGE_MARKERS = (
    "no code change required",
    "which it does",
    "already covered",
    "already satisfied",
)

POLICY_ASSERTION_TERMS = (
    "case-sensitive",
    "case sensitive",
    "case-insensitive",
    "case insensitive",
)


@dataclass(frozen=True)
class CodeTraceResult:
    """Kết quả Validator code trace V1 hybrid rule + LLM."""

    ok: bool
    summary: str
    mappings: list[dict[str, object]]
    missing_links: list[dict[str, object]]
    stats: dict[str, int]
    llm_review: dict[str, object]
    requirement_contract: dict[str, object] | None = None
    deterministic_findings: list[dict[str, object]] = field(default_factory=list)
    rejected_findings: list[dict[str, object]] = field(default_factory=list)
    review_plan: dict[str, object] = field(default_factory=dict)
    task_results: list[dict[str, object]] = field(default_factory=list)
    aggregation: dict[str, object] = field(default_factory=dict)
    cache: dict[str, object] = field(default_factory=dict)
    verification_evidence: dict[str, object] = field(default_factory=dict)
    symbol_evidence: dict[str, object] = field(default_factory=dict)
    snapshot_fingerprint: str = ""
    ledger: dict[str, object] = field(default_factory=dict)
    inconclusive: bool = False

    def to_dict(self) -> dict[str, object]:
        contract = self.requirement_contract or {"version": "v1", "revision": 0}
        return {
            "version": "requirement_design_code_trace_v1",
            "mode": CODE_VALIDATION_MODE,
            "requirement_contract": contract,
            "created_at": utc_now_iso(),
            "ok": self.ok,
            "summary": self.summary,
            "stats": self.stats,
            "mappings": self.mappings,
            "missing_links": self.missing_links,
            "deterministic_findings": self.deterministic_findings,
            "rejected_findings": self.rejected_findings,
            "review_plan": self.review_plan,
            "task_results": self.task_results,
            "aggregation": self.aggregation,
            "cache": self.cache,
            "verification_evidence": self.verification_evidence,
            "symbol_evidence": self.symbol_evidence,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "finding_ledger": self.ledger,
            "inconclusive": self.inconclusive,
            "llm_required": True,
            "llm_review": self.llm_review,
        }

    def to_quality_report(self) -> dict[str, object]:
        contract = self.requirement_contract or {"version": "v1", "revision": 0}
        symbol_files = self.symbol_evidence.get("files")
        symbol_evidence_summary = (
            {
                "version": self.symbol_evidence.get("version"),
                "extractor_id": self.symbol_evidence.get("extractor_id"),
                "profile": self.symbol_evidence.get("profile"),
                "file_count": len(symbol_files) if isinstance(symbol_files, list) else 0,
            }
            if self.symbol_evidence
            else {}
        )
        return {
            "ok": self.ok,
            "mode": CODE_VALIDATION_MODE,
            "requirement_contract_version": contract.get("version", "v2"),
            "requirement_revision": contract.get("revision", 0),
            "summary": self.summary,
            "stats": self.stats,
            "missing_count": len(self.missing_links),
            "llm_required": True,
            "llm_review_ok": self.llm_review.get("ok") is True,
            "llm_confidence": self.llm_review.get("confidence"),
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "deterministic_blocking_count": len(self.deterministic_findings),
            "semantic_blocking_count": len(_dict_items(self.aggregation.get("blocking_findings"))),
            "task_count": len(self.task_results),
            "aggregation": self.aggregation,
            "cache": self.cache,
            "symbol_evidence": symbol_evidence_summary,
            "inconclusive": self.inconclusive,
        }


class CodeTraceValidator:
    """Kiểm approved design mappings đã đi tới code/test bằng rule và LLM."""

    def __init__(
        self,
        llm_client: LlmClient,
        *,
        llm_call_budget: int = 6,
        symbol_extractor: TypeScriptAstExtractor | None = None,
        reviewable_client_factory: Callable[[dict[str, object], str], object] | None = None,
    ):
        self.llm_client = llm_client
        self.llm_call_budget = max(1, int(llm_call_budget))
        self.symbol_extractor = symbol_extractor or TypeScriptAstExtractor()
        self.reviewable_client_factory = reviewable_client_factory

    def validate(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        approved_context: dict[str, object] | None = None,
        verification_evidence: dict[str, object] | None = None,
        previous_ledger: dict[str, object] | None = None,
        code_trace_policy: CodeTracePolicy | None = None,
        task_cache_path: Path | None = None,
    ) -> CodeTraceResult:
        if code_trace_policy is None:
            raise HarnessError(
                "code_trace_policy_invalid",
                "Code Trace validation requires the immutable run policy.",
                run_id=str(work_packet.get("run_id") or "") or None,
            )
        approved_context = approved_context or {}
        verification_evidence = verification_evidence or {}
        requirement_contract = _dict_value(work_packet.get("requirement_contract"))
        v2 = requirement_contract.get("version") == "v2"
        if v2:
            approved_context = {
                **approved_context,
                "canonical_requirements": {
                    "content": json.dumps(work_packet.get("canonical_requirements"), ensure_ascii=False),
                },
            }
        mappings_by_file = _dict_of_lists(work_packet.get("design_mappings_by_file"))
        run_id = str(work_packet.get("run_id") or "").strip() or None
        test_paths = _test_paths(work_packet, policy=code_trace_policy, run_id=run_id)
        existing_test_paths = [path for path in test_paths if (target_path / path).is_file()]
        missing_test_paths = [path for path in test_paths if not (target_path / path).is_file()]
        symbol_evidence: dict[str, object] = {}
        source_paths = _profiled_source_paths(
            work_packet,
            policy=code_trace_policy,
            run_id=run_id,
        )
        ast_evidence = self.symbol_extractor.extract(
            target_path=target_path,
            source_paths=source_paths,
            policy=code_trace_policy,
            run_id=run_id,
        )
        symbol_evidence = ast_evidence.to_dict()
        trace_mappings: list[dict[str, object]] = []
        missing_links: list[dict[str, object]] = []

        if v2:
            trace_mappings, coverage_missing = _v2_trace_mappings(
                target_path=target_path,
                work_packet=work_packet,
                existing_test_paths=existing_test_paths,
                code_trace_policy=code_trace_policy,
                ast_evidence=ast_evidence,
                run_id=run_id,
            )
            missing_links.extend(coverage_missing)
        elif not mappings_by_file:
            missing_links.append(
                {
                    "type": "missing_design_mappings",
                    "message": "DeveloperWorkPacket has no design_mappings_by_file.",
                }
            )

        if not test_paths:
            missing_links.append(
                {
                    "type": "missing_test_mapping",
                    "message": "DeveloperWorkPacket has no proposed test files for code trace.",
                }
            )
        elif missing_test_paths:
            missing_links.append(
                {
                    "type": "missing_test_file",
                    "message": "One or more proposed test files do not exist in the target repo after Developer.act.",
                    "missing_test_files": missing_test_paths,
                }
            )

        for raw_source_path, design_items in ([] if v2 else mappings_by_file.items()):
            for item in design_items:
                source_path = raw_source_path
                source_file = target_path / source_path
                source_exists = source_file.is_file()
                source_text = source_file.read_text(encoding="utf-8", errors="replace") if source_exists else ""
                if not source_exists:
                    missing_links.append(
                        {
                            "type": "missing_source_file",
                            "source_file": source_path,
                            "message": "Mapped source file does not exist in the target repo.",
                        }
                    )
                planned_target = str(item.get("planned_code_target") or source_path).strip()
                raw_symbol = _symbol_from_target(planned_target)
                symbol = raw_symbol
                symbol_found = (
                    True
                    if not symbol
                    else (
                        source_exists and symbol in ast_evidence.symbols_for(source_path)
                        if ast_evidence is not None
                        else symbol in source_text
                    )
                )
                design_element_id = str(item.get("design_element_id") or "").strip()
                requirement_ref = _requirement_ref(item)
                trace_mappings.append(
                    {
                        "requirement_ref": requirement_ref,
                        "design_element_id": design_element_id,
                        "design_element_type": str(item.get("design_element_type") or "").strip(),
                        "design_element_name": str(item.get("design_element_name") or "").strip(),
                        "planned_code_target": planned_target,
                        "source_file": source_path,
                        "source_exists": source_exists,
                        "symbol": symbol,
                        "raw_symbol": raw_symbol,
                        "symbol_found": symbol_found,
                        "symbol_evidence_source": ast_evidence.extractor_id,
                        "requires_direct_symbol": bool(symbol),
                        "trace_policy": _trace_policy(raw_symbol, item),
                        "mapping_note": _mapping_note(raw_symbol, item),
                        "test_files": existing_test_paths,
                        "test_covered": bool(existing_test_paths),
                    }
                )

        if work_packet.get("schema_version") in {
            PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
            UNIT_AWARE_PROFILED_DEVELOPER_WORK_PACKET_SCHEMA_VERSION,
        }:
            deterministic_findings = deterministic_typescript_contract_findings(
                work_packet=work_packet,
                ast_evidence=ast_evidence,
            )
        else:
            deterministic_findings = []
        static_missing_links = [*missing_links, *deterministic_findings]
        validation_claims = build_validation_claims(
            work_packet=work_packet,
            trace_mappings=trace_mappings,
        )
        code_snippets = _code_snippets(
            target_path=target_path,
            mappings=trace_mappings,
            test_paths=existing_test_paths,
        )
        fingerprint = snapshot_fingerprint(
            work_packet=work_packet,
            code_snippets=code_snippets,
            approved_context=approved_context,
            verification_evidence=verification_evidence,
            symbol_evidence=symbol_evidence,
        )
        try:
            review_plan = build_code_validation_review_plan(
                work_packet=work_packet,
                approved_context=approved_context,
                validation_claims=validation_claims,
            )
        except ValueError as exc:
            raise HarnessError(
                "implementation_validation_plan_invalid",
                str(exc),
                run_id=run_id,
            ) from exc
        tasks = _dict_items(review_plan.get("tasks"))
        if not static_missing_links and self.llm_call_budget < len(tasks):
            raise HarnessError(
                "implementation_validation_call_budget_insufficient",
                (
                    f"Code validation requires {len(tasks)} task call(s), but the configured "
                    f"budget is {self.llm_call_budget}."
                ),
                run_id=run_id,
            )

        task_cache = CodeValidationTaskCache(task_cache_path)
        task_results: list[dict[str, object]] = []
        semantic_blockers: list[dict[str, object]] = []
        observations: list[dict[str, object]] = []
        rejected_findings: list[dict[str, object]] = []
        calls_used = 0
        cache_hits = 0
        cache_misses = 0
        inconclusive = False
        inconclusive_task_id = ""
        invalid_tasks: list[dict[str, object]] = []
        if not static_missing_links:
            for task in tasks:
                task_id = str(task.get("task_id") or "").strip()
                task_snippets = _task_code_snippets(
                    target_path=target_path,
                    task=task,
                    ast_evidence=ast_evidence,
                    fallback_test_paths=existing_test_paths,
                )
                prompt = _code_validation_task_prompt(
                    task=task,
                    code_snippets=task_snippets,
                    trace_mappings=trace_mappings,
                    verification_evidence=verification_evidence,
                    symbol_evidence=symbol_evidence,
                    scenario_results=task_results,
                    approved_closure_changes=_dict_value(approved_context.get("approved_closure_changes")) or None,
                )
                cached = task_cache.get(prompt)
                if cached is not None:
                    try:
                        _validate_code_validation_task_output(
                            output=cached,
                            task=task,
                            prompt=prompt,
                        )
                    except ValueError:
                        cached = None
                if cached is not None:
                    review = cached
                    metadata = {
                        "cache_hit": True,
                        "prompt_chars": len(prompt),
                        "attempts": [],
                    }
                    cache_hits += 1
                else:
                    cache_misses += 1
                    max_attempts = min(2, self.llm_call_budget - calls_used)
                    if max_attempts < 1:
                        inconclusive = True
                        inconclusive_task_id = task_id
                        invalid_tasks.append(
                            {"task_id": task_id, "message": "LLM call budget exhausted before this task."}
                        )
                        break
                    try:
                        review, metadata = self._complete_task_review_json(
                            task=task,
                            prompt=prompt,
                            max_attempts=max_attempts,
                        )
                    except LlmStepReviewRequired:
                        raise
                    except HarnessError as exc:
                        calls_used += max_attempts
                        inconclusive = True
                        inconclusive_task_id = task_id
                        invalid_tasks.append({"task_id": task_id, "message": exc.message})
                        break
                    calls_used += max(1, int(metadata.get("selected_attempt") or 1))
                    task_cache.put(prompt, review)

                findings, task_observations = _task_findings(review=review, task=task)
                semantic_blockers.extend(findings)
                observations.extend(task_observations)
                task_results.append(
                    {
                        "task_id": task_id,
                        "task_kind": task.get("task_kind"),
                        "scenario_ids": task.get("scenario_ids"),
                        "unit_ids": task.get("unit_ids"),
                        "reviewed_claim_ids": review.get("reviewed_claim_ids"),
                        "reviewed_design_ids": review.get("reviewed_design_ids"),
                        "reviewed_boundary_contract_ids": review.get("reviewed_boundary_contract_ids"),
                        "reported_ok": review.get("ok") is True,
                        "summary": review.get("summary"),
                        "blocking_findings": findings,
                        "observations": task_observations,
                        "llm_metadata": metadata,
                    }
                )

        missing_links = [*static_missing_links, *semantic_blockers]
        static_ok = bool(trace_mappings) and not static_missing_links
        completed_task_ids = {str(item.get("task_id") or "") for item in task_results}
        expected_task_ids = {str(item.get("task_id") or "") for item in tasks}
        all_tasks_completed = static_missing_links or completed_task_ids == expected_task_ids
        ok = static_ok and all_tasks_completed and not semantic_blockers and not inconclusive
        aggregation = {
            "policy": CODE_VALIDATION_MODE,
            "task_count": len(tasks),
            "completed_task_count": len(task_results),
            "blocking_findings": semantic_blockers,
            "observation_count": len(observations),
            "llm_call_budget": self.llm_call_budget,
            "llm_calls_used": calls_used,
            "inconclusive": inconclusive,
            "inconclusive_task_id": inconclusive_task_id,
            "invalid_tasks": invalid_tasks,
            "deterministic_preflight_blocked": bool(static_missing_links),
        }
        llm_review = {
            "ok": not semantic_blockers and not inconclusive and all_tasks_completed,
            "reported_ok": all(item.get("reported_ok") is True for item in task_results),
            "summary": _task_review_summary(
                task_results=task_results,
                blockers=semantic_blockers,
                inconclusive=inconclusive,
                deterministic_blocked=bool(static_missing_links),
            ),
            "missing_links": semantic_blockers,
            "observations": observations,
            "risks": [],
            "review_context": {
                "task_count": len(tasks),
                "completed_task_count": len(task_results),
                "code_snippets_included": bool(task_results),
            },
            "normalization": {
                "blocking_count": len(semantic_blockers),
                "observation_count": len(observations),
                "rejected_count": len(rejected_findings),
                "python_recomputed_ok": True,
            },
        }
        ledger = build_finding_ledger(
            previous=previous_ledger,
            snapshot_id=fingerprint,
            active_findings=missing_links,
            disputed_findings=[],
            rejected_findings=rejected_findings,
        )
        stats = {
            "mapping_count": len(trace_mappings),
            "missing_count": len(missing_links),
            "test_file_count": len(existing_test_paths),
            "observation_count": len(observations),
            "requirement_count": len(_dict_items(work_packet.get("canonical_requirements"))) if v2 else 0,
            "requirements_covered": len(trace_mappings) if v2 else 0,
        }
        trace_label = "V2" if v2 else "V1"
        if ok:
            summary = (
                f"Validator code trace {trace_label} passed with static evidence, LLM graph review, "
                f"{len(trace_mappings)} mapping(s), {len(existing_test_paths)} test file(s), "
                f"and {len(observations)} non-blocking observation(s) across {len(task_results)} semantic task(s)."
            )
        elif inconclusive:
            summary = (
                f"Validator code trace {trace_label} task {inconclusive_task_id} was inconclusive "
                f"after {calls_used} LLM call(s); "
                "no Developer repair feedback was authorized."
            )
        else:
            llm_summary = str(llm_review.get("summary") or "").strip()
            detail = f" LLM review: {llm_summary}" if llm_summary else ""
            summary = f"Validator code trace {trace_label} found {len(missing_links)} missing link(s).{detail}"
        return CodeTraceResult(
            ok=ok,
            summary=summary,
            mappings=trace_mappings,
            missing_links=missing_links,
            stats=stats,
            llm_review=llm_review,
            requirement_contract=requirement_contract,
            deterministic_findings=deterministic_findings,
            rejected_findings=rejected_findings,
            review_plan=review_plan,
            task_results=task_results,
            aggregation=aggregation,
            cache={
                "schema_version": "code_validation_task_cache_v1",
                "hits": cache_hits,
                "misses": cache_misses,
            },
            verification_evidence=verification_evidence,
            symbol_evidence=symbol_evidence,
            snapshot_fingerprint=fingerprint,
            ledger=ledger,
            inconclusive=inconclusive,
        )

    def _complete_task_review_json(
        self,
        *,
        task: dict[str, object],
        prompt: str,
        max_attempts: int,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Run one authoritative task and retry once with the exact contract error."""

        system = (
            "You are the semantic Code Validator for one explicitly bounded task. "
            "Judge requirement/design meaning; do not review claims owned by another task. "
            "Return only valid JSON."
        )
        llm_client = (
            self.reviewable_client_factory(task, prompt)
            if self.reviewable_client_factory is not None
            else self.llm_client
        )
        settings = getattr(llm_client, "settings", None)
        request_options = phase4_llm_call_options(
            settings,
            "code_validator",
            fallback_max_tokens=int(getattr(settings, "llm_structured_max_tokens", 8192)),
        )
        max_tokens = int(request_options.get("max_tokens") or 8192)
        completion_method = getattr(llm_client, "complete_with_metadata", None)
        attempts: list[dict[str, object]] = []
        attempt_prompt = prompt
        last_error = "Code validation task did not return a valid JSON object."

        for attempt in range(1, max(1, max_attempts) + 1):
            review: dict[str, object] | None = None
            if callable(completion_method):
                completion = completion_method(
                    system=system,
                    prompt=attempt_prompt,
                    json_mode=True,
                    **request_options,
                )
                raw = str(getattr(completion, "content", ""))
                finish_reason = str(getattr(completion, "finish_reason", "") or "")
                usage_value = getattr(completion, "usage", {})
                usage = usage_value if isinstance(usage_value, dict) else {}
                json_mode_requested = True
            else:
                raw = str(llm_client.complete(system=system, prompt=attempt_prompt))
                finish_reason = ""
                usage = {}
                json_mode_requested = False

            metadata: dict[str, object] = {
                "attempt": attempt,
                "finish_reason": finish_reason,
                "usage": usage,
                "json_mode_requested": json_mode_requested,
                "max_tokens": max_tokens,
                "prompt_chars": len(attempt_prompt),
            }
            if finish_reason == "length":
                last_error = "Code validation task output was truncated (finish_reason=length)."
                metadata["parse_status"] = "truncated"
            else:
                try:
                    review = parse_first_json_object(
                        raw,
                        expected_keys={
                            "task_id",
                            "reviewed_claim_ids",
                            "reviewed_design_ids",
                            "reviewed_boundary_contract_ids",
                            "ok",
                            "summary",
                            "findings",
                            "observations",
                        },
                    )
                except json.JSONDecodeError:
                    metadata["parse_status"] = "invalid"
                    last_error = "Response is not one complete JSON object with the required keys."
                else:
                    try:
                        _validate_code_validation_task_output(
                            output=review,
                            task=task,
                            prompt=prompt,
                        )
                    except ValueError as exc:
                        metadata["parse_status"] = "contract_invalid"
                        last_error = str(exc)
                    else:
                        metadata["parse_status"] = "valid"
                        attempts.append(metadata)
                        return review, {
                            "json_mode_requested": json_mode_requested,
                            "max_tokens": max_tokens,
                            "selected_attempt": attempt,
                            "attempts": attempts,
                            "prompt_chars": len(prompt),
                            "cache_hit": False,
                        }
            attempts.append(metadata)
            attempt_prompt = _code_validation_recovery_prompt(
                prompt=prompt,
                previous_output=review,
                contract_error=last_error,
            )

        raise HarnessError(
            "implementation_traceability_llm_failed",
            f"{last_error} Task output failed after {len(attempts)} attempt(s).",
        )


def _code_validation_recovery_prompt(
    *,
    prompt: str,
    previous_output: dict[str, object] | None,
    contract_error: str,
) -> str:
    """Retry a parsed contract failure without resending the whole task packet."""

    if previous_output is None:
        return (
            prompt
            + "\n\nCONTRACT RECOVERY RETRY\n"
            + f"The previous response was rejected: {contract_error}\n"
            + "Return one complete JSON object that follows expected_json_schema exactly. "
            + "Do not add claims or IDs."
        )
    try:
        payload = json.loads(prompt)
    except json.JSONDecodeError:
        return prompt

    finding = next(
        (
            item
            for item in _dict_items(previous_output.get("findings"))
            if str(item.get("finding_id") or "") in contract_error
        ),
        {},
    )
    claim_ids = set(_string_items(finding.get("claim_ids")))
    design_ids = set(_string_items(finding.get("design_refs")))
    requirement_refs = set(_string_items(finding.get("requirement_refs")))
    code_paths = {_target_path(value) for value in _string_items(finding.get("code_refs"))}
    test_paths = {_target_path(value) for value in _string_items(finding.get("test_refs"))}
    cited_paths = code_paths | test_paths
    evidence_ref = str(finding.get("evidence_ref") or "").casefold()

    relevant_evidence: dict[str, object] = {}
    claims = [
        item
        for item in _dict_items(payload.get("authority_claims"))
        if str(item.get("claim_id") or "") in claim_ids
    ]
    if claims:
        relevant_evidence["authority_claims"] = claims
    contracts = [
        item
        for item in _dict_items(payload.get("approved_design_contracts"))
        if str(item.get("id") or "") in design_ids
    ]
    if contracts:
        relevant_evidence["approved_design_contracts"] = contracts
    mappings = [
        item
        for item in _dict_items(payload.get("static_trace_mappings"))
        if str(item.get("requirement_ref") or "") in requirement_refs
    ]
    if mappings:
        relevant_evidence["static_trace_mappings"] = mappings
        mapped_targets = {
            value
            for item in mappings
            for value in _string_items(item.get("planned_code_targets"))
        }
        trace_evidence = _dict_value(payload.get("static_trace_evidence"))
        code_evidence = [
            item
            for item in _dict_items(trace_evidence.get("code_evidence"))
            if str(item.get("planned_code_target") or "") in mapped_targets
            or str(item.get("source_file") or "").replace("\\", "/") in cited_paths
        ]
        shared_test_files = sorted(
            set(_string_items(trace_evidence.get("test_files")))
        )
        if code_evidence or shared_test_files:
            relevant_evidence["static_trace_evidence"] = {
                "code_evidence": code_evidence,
                "test_files": shared_test_files,
                **(
                    {"join_rule": trace_evidence.get("join_rule")}
                    if trace_evidence.get("join_rule")
                    else {}
                ),
            }
    snippets = [
        item
        for item in _dict_items(payload.get("code_snippets"))
        if str(item.get("path") or "").replace("\\", "/") in cited_paths
    ]
    if snippets:
        relevant_evidence["code_snippets"] = snippets
    symbol_evidence = _dict_value(payload.get("symbol_evidence"))
    symbol_files = [
        item
        for item in _dict_items(symbol_evidence.get("files"))
        if str(item.get("path") or "").replace("\\", "/") in code_paths
    ]
    if symbol_files:
        relevant_evidence["symbol_evidence"] = {
            key: symbol_evidence.get(key)
            for key in ("version", "extractor_id", "profile")
            if symbol_evidence.get(key) is not None
        } | {"files": symbol_files}
    if "verification" in evidence_ref:
        relevant_evidence["verification_evidence"] = payload.get("verification_evidence")

    task_unit_ids = set(_string_items(_dict_value(payload.get("task_contract")).get("unit_ids")))
    relevant_units: list[dict[str, object]] = []
    for unit in _dict_items(payload.get("approved_units")):
        unit_id = str(unit.get("unit_id") or "").strip()
        if unit_id not in task_unit_ids:
            continue
        code_targets = [
            {
                "path": str(target.get("path") or "").strip().replace("\\", "/"),
                **(
                    {"symbol": str(target.get("symbol") or "").strip()}
                    if str(target.get("symbol") or "").strip()
                    else {}
                ),
            }
            for target in _dict_items(unit.get("planned_code_targets"))
            if str(target.get("path") or "").strip()
        ]
        projected_test_paths = _string_items(unit.get("test_paths"))
        test_paths = sorted(
            {
                str(path).strip().replace("\\", "/")
                for path in (
                    projected_test_paths
                    if "test_paths" in unit
                    else [
                        path
                        for intent in _dict_items(unit.get("test_intents"))
                        if str(intent.get("stage_id") or "").strip() != "typecheck"
                        for path in _string_items(intent.get("target_paths"))
                    ]
                )
                if str(path).strip()
            }
        )
        relevant_units.append(
            {
                "unit_id": unit_id,
                **({"title": unit.get("title")} if unit.get("title") else {}),
                **({"objective": unit.get("objective")} if unit.get("objective") else {}),
                "code_targets": code_targets,
                "test_paths": test_paths,
            }
        )

    return json.dumps(
        {
            "prompt_schema_version": (
                "code_validation_contract_recovery_prompt_v6"
                if payload.get("prompt_schema_version") == "code_validation_task_prompt_v6"
                else "code_validation_contract_recovery_prompt_v4"
            ),
            "task": "Code Validation Contract Recovery v1",
            "instruction": (
                "Correct only the stated contract defect in previous_output. Preserve every semantic decision, "
                "finding, severity and observation unless that exact field must change to satisfy contract_error. "
                "Do not remove a finding to bypass validation. Use relevant_units only to select exact unit_ids; "
                "it is implementation scope, not additional semantic authority. Return the complete corrected "
                "JSON object only."
            ),
            "contract_error": contract_error,
            "task_contract": payload.get("task_contract"),
            "expected_json_schema": payload.get("expected_json_schema"),
            "previous_output": previous_output,
            "relevant_evidence": relevant_evidence,
            "relevant_units": relevant_units,
        },
        ensure_ascii=False,
        indent=0,
    )

def _v2_trace_mappings(
    *,
    target_path: Path,
    work_packet: dict[str, object],
    existing_test_paths: list[str],
    code_trace_policy: CodeTracePolicy,
    ast_evidence: TypeScriptAstEvidence,
    run_id: str | None = None,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Build one deterministic code-trace row for each canonical stable requirement ID."""

    canonical = _dict_items(work_packet.get("canonical_requirements"))
    coverage = _dict_items(work_packet.get("requirement_coverage"))
    expected = {
        str(item.get("id") or "").strip(): str(item.get("content") or "").strip()
        for item in canonical
        if str(item.get("id") or "").strip()
    }
    actual_ids = [str(item.get("requirement_id") or "").strip() for item in coverage]
    if not expected or len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
        raise HarnessError(
            "code_trace_requirement_coverage_invalid",
            "DeveloperWorkPacket must contain each canonical stable requirement ID exactly once.",
            run_id=run_id,
        )
    mappings_by_file = _dict_of_lists(work_packet.get("design_mappings_by_file"))
    design_by_id = {
        str(item.get("design_element_id") or "").strip(): item
        for items in mappings_by_file.values()
        for item in items
        if str(item.get("design_element_id") or "").strip()
    }
    trace_mappings: list[dict[str, object]] = []
    missing_links: list[dict[str, object]] = []
    for item in coverage:
        requirement_id = str(item.get("requirement_id") or "").strip()
        content = str(item.get("content") or "").strip()
        targets = _string_items(item.get("planned_code_targets"))
        design_ids = _string_items(item.get("design_element_ids"))
        test_intents = _string_items(item.get("test_intents"))
        if content != expected[requirement_id] or not targets or not design_ids or not test_intents:
            raise HarnessError(
                "code_trace_requirement_coverage_invalid",
                f"Requirement coverage is incomplete or stale for {requirement_id}.",
                run_id=run_id,
            )
        evidence: list[dict[str, object]] = []
        for target in targets:
            raw_source_path = target.split("#", 1)[0].strip().replace("\\", "/")
            design_item = next(
                (
                    design_by_id[design_id]
                    for design_id in design_ids
                    if design_id in design_by_id
                    and str(design_by_id[design_id].get("planned_code_target") or "").strip() == target
                ),
                {"planned_code_target": target},
            )
            source_path = raw_source_path
            source_file = target_path / source_path
            source_exists = source_file.is_file()
            source_text = source_file.read_text(encoding="utf-8", errors="replace") if source_exists else ""
            raw_symbol = _symbol_from_target(target)
            symbol = raw_symbol
            symbol_found = source_exists and (
                not symbol
                or (
                    symbol in ast_evidence.symbols_for(source_path)
                    if ast_evidence is not None
                    else symbol in source_text
                )
            )
            evidence.append(
                {
                    "planned_code_target": target,
                    "source_file": source_path,
                    "source_exists": source_exists,
                    "symbol": symbol,
                    "raw_symbol": raw_symbol,
                    "symbol_found": symbol_found,
                    "symbol_evidence_source": ast_evidence.extractor_id,
                }
            )
            if not source_exists:
                missing_links.append(
                    {
                        "type": "missing_source_file",
                        "requirement_ref": requirement_id,
                        "source_file": source_path,
                        "message": f"Canonical requirement {requirement_id} has no source-file evidence.",
                    }
                )
            elif symbol and not symbol_found:
                missing_links.append(
                    {
                        "type": "missing_code_symbol",
                        "requirement_ref": requirement_id,
                        "source_file": source_path,
                        "symbol": symbol,
                        "message": f"Canonical requirement {requirement_id} has no planned symbol evidence.",
                    }
                )
        trace_mappings.append(
            {
                "requirement_ref": requirement_id,
                "requirement_content": content,
                "design_element_ids": design_ids,
                "design_targets": _string_items(item.get("design_targets")),
                "planned_code_targets": targets,
                "code_evidence": evidence,
                "source_file": str(evidence[0].get("source_file") or "") if evidence else "",
                "symbol": str(evidence[0].get("symbol") or "") if evidence else "",
                "symbol_found": all(bool(value.get("symbol_found")) for value in evidence),
                "test_intents": test_intents,
                "test_files": existing_test_paths,
                "test_covered": bool(existing_test_paths),
                "evidence_status": "covered"
                if evidence and all(bool(value.get("symbol_found")) for value in evidence) and existing_test_paths
                else "missing",
            }
        )
    return trace_mappings, missing_links


def _dict_of_lists(value: object) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for key, items in value.items():
        if not isinstance(items, list):
            continue
        normalized = [item for item in items if isinstance(item, dict)]
        if normalized:
            result[str(key).strip().replace("\\", "/")] = normalized
    return result


def _dict_value(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _test_paths(
    work_packet: dict[str, object],
    *,
    policy: CodeTracePolicy,
    run_id: str | None = None,
) -> list[str]:
    paths: list[str] = []
    candidates: list[object] = []
    for item in _dict_items(work_packet.get("proposed_files")):
        candidates.append(item.get("path"))
    contract = work_packet.get("output_contract")
    allowed = contract.get("allowed_paths") if isinstance(contract, dict) else None
    if isinstance(allowed, list):
        candidates.extend(allowed)
    for item in candidates:
        path = policy.normalize_test_path(item)
        if path:
            paths.append(path)
            continue
        raw_path = str(item or "")
        editable_path = policy.planning_policy.normalize_editable_path(item)
        component_id = policy.planning_policy.component_for_path(editable_path) if editable_path else ""
        looks_like_test = raw_path.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx"))
        if looks_like_test or component_id == "e2e":
            raise HarnessError(
                "code_trace_test_out_of_scope",
                f"Code Trace test path is outside the profile policy: {raw_path}",
                run_id=run_id,
            )
    return sorted({path for path in paths if path})


def _profiled_source_paths(
    work_packet: dict[str, object],
    *,
    policy: CodeTracePolicy,
    run_id: str | None,
) -> list[str]:
    """Thu thập và kiểm tra mọi production source path trước khi trích xuất AST."""

    candidates: list[object] = list(_dict_of_lists(work_packet.get("design_mappings_by_file")))
    for item in _dict_items(work_packet.get("requirement_coverage")):
        candidates.extend(target.split("#", 1)[0] for target in _string_items(item.get("planned_code_targets")))
    paths: list[str] = []
    for item in candidates:
        path = policy.normalize_source_path(item)
        if not path:
            raise HarnessError(
                "code_trace_source_out_of_scope",
                f"Code Trace source path is outside the profile policy: {item}",
                run_id=run_id,
            )
        if path not in paths:
            paths.append(path)
    return paths


def _dict_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _symbol_from_target(planned_target: str) -> str:
    if "#" not in planned_target:
        return ""
    return planned_target.split("#", 1)[1].strip()


def _trace_policy(symbol: str, item: dict[str, Any]) -> str:
    lower_symbol = symbol.strip().lower()
    text = _mapping_text(item).lower()
    if lower_symbol in {"true", "false"} or "->dc: true" in text or "->dc: false" in text:
        return "boolean_response"
    if "display" in text or "notify" in text:
        return "semantic_side_effect"
    return "direct_symbol" if symbol.strip() else "file_or_test_evidence"


def _mapping_note(symbol: str, item: dict[str, Any]) -> str:
    policy = _trace_policy(symbol, item)
    if policy == "boolean_response":
        return "Boolean sequence response; validate surrounding branch behavior/test assertions, not a literal true/false symbol."
    if policy == "semantic_side_effect":
        return "Notification/display sequence side effect; validate NotificationService behavior and tests, not a literal display* method."
    return ""


def _mapping_text(item: dict[str, Any]) -> str:
    return " ".join(
        str(item.get(key) or "")
        for key in ("design_element_name", "planned_code_target", "design_element_id", "design_element_type")
    )


def _requirement_ref(item: dict[str, object]) -> str:
    for key in ("requirement_id", "requirement_ref", "requirement", "source_requirement"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return "approved_requirement_packet"


def _normalize_llm_findings(
    review: dict[str, object],
    *,
    approved_context: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Chỉ giữ finding blocking khi có action và trích dẫn approved evidence hợp lệ."""

    value = review.get("missing_links")
    if not isinstance(value, list):
        return [], []
    approved_text = _approved_grounding_text(approved_context)
    blocking: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            original_type = str(item.get("type") or "").strip()
            normalized_item = dict(item)
            normalized_item["type"] = (
                original_type if original_type in NON_REPAIRABLE_LLM_LINK_TYPES else "llm_semantic_gap"
            )
            if original_type and normalized_item["type"] != original_type:
                normalized_item["llm_type"] = original_type
            message = str(item.get("message") or "").strip()
            message_lower = message.casefold()
            owner = str(item.get("owner") or "developer").strip().lower()
            action = str(item.get("recommended_action") or "developer_repair").strip().lower()
            evidence_ref = str(item.get("evidence_ref") or "").strip()
            evidence_quote = str(item.get("evidence_quote") or "").strip()
            explicit_blocking = item.get("blocking") is True
            explicitly_non_blocking = item.get("blocking") is False
            no_action = owner == "none" or action == "none"
            self_resolved = any(marker in message_lower for marker in NON_BLOCKING_MESSAGE_MARKERS)
            grounded = bool(evidence_ref and evidence_quote and _quote_is_grounded(evidence_quote, approved_text))

            normalized_item.update(
                {
                    "blocking": explicit_blocking,
                    "owner": owner,
                    "recommended_action": action,
                    "evidence_ref": evidence_ref,
                    "evidence_quote": evidence_quote,
                }
            )
            if explicitly_non_blocking or no_action or self_resolved:
                normalized_item["blocking"] = False
                normalized_item["normalization_reason"] = "model_marked_non_blocking_or_self_resolved"
                observations.append(normalized_item)
            elif not explicit_blocking:
                normalized_item["blocking"] = False
                normalized_item["normalization_reason"] = "blocking_flag_missing"
                observations.append(normalized_item)
            elif not grounded:
                normalized_item["blocking"] = False
                normalized_item["normalization_reason"] = "approved_evidence_missing_or_unsupported"
                observations.append(normalized_item)
            elif _contains_unsupported_policy_claim(message, approved_text):
                normalized_item["blocking"] = False
                normalized_item["normalization_reason"] = "policy_term_absent_from_approved_artifacts"
                observations.append(normalized_item)
            elif _finding_order_contradicts_evidence(message, evidence_quote):
                normalized_item["blocking"] = False
                normalized_item["owner"] = "validator"
                normalized_item["recommended_action"] = "validator_mapping_fix"
                normalized_item["normalization_reason"] = "finding_order_contradicts_approved_evidence"
                observations.append(normalized_item)
            elif _finding_equates_intermediate_state_with_terminal_success(
                message,
                evidence_quote,
                approved_text,
            ):
                normalized_item["blocking"] = False
                normalized_item["owner"] = "validator"
                normalized_item["recommended_action"] = "validator_mapping_fix"
                normalized_item["normalization_reason"] = (
                    "finding_equates_intermediate_state_with_terminal_success"
                )
                observations.append(normalized_item)
            elif _finding_blocks_on_intermediate_representation_only(
                message,
                evidence_quote,
            ):
                normalized_item["blocking"] = False
                normalized_item["owner"] = "validator"
                normalized_item["recommended_action"] = "validator_mapping_fix"
                normalized_item["normalization_reason"] = (
                    "intermediate_representation_without_missing_behavior"
                )
                observations.append(normalized_item)
            elif owner == "human" or action == "human_decision":
                normalized_item["type"] = "open_requirement_question"
                blocking.append(normalized_item)
            else:
                blocking.append(normalized_item)
        elif str(item).strip():
            observations.append(
                {
                    "type": "llm_observation",
                    "blocking": False,
                    "owner": "none",
                    "recommended_action": "none",
                    "message": str(item).strip(),
                    "normalization_reason": "unstructured_finding",
                }
            )
    return blocking, observations


def _approved_grounding_text(approved_context: dict[str, object]) -> str:
    parts: list[str] = []
    for key in ("canonical_requirements", "requirement_packet", "design_artifact", "implementation_plan"):
        value = approved_context.get(key)
        if isinstance(value, dict):
            content = str(value.get("content") or "").strip()
        else:
            content = str(value or "").strip()
        if content:
            parts.append(content)
    return _normalized_evidence_text("\n".join(parts))


def _quote_is_grounded(quote: str, approved_text: str) -> bool:
    normalized_quote = _normalized_evidence_text(quote)
    return bool(normalized_quote and approved_text and normalized_quote in approved_text)


def _contains_unsupported_policy_claim(message: str, approved_text: str) -> bool:
    normalized_message = _normalized_evidence_text(message)
    return any(
        term in normalized_message and term not in approved_text
        for term in POLICY_ASSERTION_TERMS
    )


def _finding_order_contradicts_evidence(message: str, evidence_quote: str) -> bool:
    """Reject an order claim when its cited approved quote shows the reverse order."""

    anchors = list(
        re.finditer(
            r"\b(?:approved|requires?|required|must|should|expected)\b",
            message,
            flags=re.IGNORECASE,
        )
    )
    if not anchors:
        return False
    expected_operations = _ordered_call_tokens(message[anchors[-1].start() :])
    evidence_operations = _ordered_call_tokens(evidence_quote)
    common = [operation for operation in expected_operations if operation in evidence_operations]
    common = list(dict.fromkeys(common))
    if len(common) < 2:
        return False
    evidence_common = list(dict.fromkeys(operation for operation in evidence_operations if operation in common))
    return evidence_common[: len(common)] != common


def _ordered_call_tokens(value: str) -> list[str]:
    """Extract qualified call names while preserving their textual order."""

    return [
        re.sub(r"\s+", "", match.group(1)).casefold()
        for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\s*\(", value)
    ]


def _finding_equates_intermediate_state_with_terminal_success(
    message: str,
    evidence_quote: str,
    approved_text: str,
) -> bool:
    """Reject a claim that turns an approved intermediate state into terminal success."""

    normalized_message = _normalized_evidence_text(message)
    normalized_quote = _normalized_evidence_text(evidence_quote)
    if not normalized_message or not normalized_quote or not approved_text:
        return False

    success_claims = (
        "as a success result",
        "as success result",
        "must return success",
        "should return success",
        "must be success",
        "should be success",
        "success=true",
        "success = true",
    )
    intermediate_markers = (
        "valid",
        "pending",
        "awaiting",
        "requestconfirmation",
        "request confirmation",
        "confirmation required",
        "in progress",
        "processing",
    )
    terminal_markers = (
        "result(success",
        "successfully inserted",
        "successfully stored",
        "successfully saved",
        "success notification",
    )
    if not any(claim in normalized_message for claim in success_claims):
        return False
    if not any(marker in normalized_quote for marker in intermediate_markers):
        return False
    if any(marker in normalized_quote for marker in terminal_markers):
        return False

    quote_index = approved_text.find(normalized_quote)
    if quote_index < 0:
        return False
    approved_after_quote = approved_text[quote_index + len(normalized_quote) :]
    return any(marker in approved_after_quote for marker in terminal_markers)


def _finding_blocks_on_intermediate_representation_only(
    message: str,
    evidence_quote: str,
) -> bool:
    """Keep representation debt non-blocking when the approved transition still occurs."""

    normalized_message = _normalized_evidence_text(message)
    normalized_quote = _normalized_evidence_text(evidence_quote)
    if not normalized_message or not normalized_quote:
        return False

    intermediate_markers = (
        "valid",
        "pending",
        "awaiting",
        "requestconfirmation",
        "request confirmation",
        "confirmation required",
        "in progress",
        "processing",
    )
    representation_markers = (
        "issuccess() is false",
        "issuccess is false",
        "failure-typed result",
        "failure typed result",
        "result type semantics",
        "factory semantics",
        "string-matching",
        "string matching",
        "literal message",
    )
    missing_behavior_markers = (
        "does not request confirmation",
        "never requests confirmation",
        "confirmation is not requested",
        "cannot confirm",
        "does not reach confirm",
        "never reaches confirm",
        "prevents confirmation",
    )
    if not any(marker in normalized_quote for marker in intermediate_markers):
        return False
    if "success" in normalized_quote:
        return False
    if any(marker in normalized_message for marker in missing_behavior_markers):
        return False
    return any(marker in normalized_message for marker in representation_markers)


def _normalized_evidence_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _project_verification_evidence(
    verification_evidence: dict[str, object],
    *,
    task_paths: set[str],
    test_paths: set[str],
) -> dict[str, object]:
    """Keep task-relevant, stable proof so unrelated repairs preserve cache hits."""

    def selected_paths(value: object) -> list[str]:
        return [path for path in _string_items(value) if path.replace("\\", "/") in task_paths]

    def stage(item: dict[str, object]) -> dict[str, object]:
        return {
            key: item.get(key)
            for key in (
                "stage_id",
                "status",
                "argv",
                "cwd",
                "exit_code",
                "reason",
                "failure_kind",
                "timed_out",
            )
            if item.get(key) not in (None, "")
        } | {
            "selected_test_files": selected_paths(item.get("selected_test_files")),
            "verified_test_files": selected_paths(item.get("verified_test_files")),
        }

    verification_output = "\n".join(
        str(value)
        for value in [
            verification_evidence.get("stdout"),
            verification_evidence.get("stderr"),
            *[
                stage_item.get(key)
                for stage_item in _dict_items(verification_evidence.get("stage_results"))
                for key in ("stdout", "stderr")
            ],
        ]
        if value
    ).replace("\\", "/").casefold()
    observed_test_files = sorted(
        path for path in test_paths if path.replace("\\", "/").casefold() in verification_output
    )

    return {
        key: verification_evidence.get(key)
        for key in (
            "status",
            "exit_code",
            "reason",
            "failure_kind",
            "timed_out",
            "discovered_test_entrypoints",
            "executed_test_entrypoints",
            "planned_stage_ids",
            "executed_stage_ids",
        )
        if verification_evidence.get(key) not in (None, "")
    } | {
        "selected_test_files": selected_paths(verification_evidence.get("selected_test_files")),
        "verified_test_files": selected_paths(verification_evidence.get("verified_test_files")),
        "observed_task_test_files": observed_test_files,
        "stage_results": [stage(item) for item in _dict_items(verification_evidence.get("stage_results"))],
        "preparation_results": [
            stage(item) for item in _dict_items(verification_evidence.get("preparation_results"))
        ],
    }


def _project_task_trace_mapping(
    item: dict[str, object],
    *,
    design_ids: set[str],
    task_paths: set[str],
) -> dict[str, object]:
    return {
        "requirement_ref": item.get("requirement_ref"),
        "design_element_ids": [
            value for value in _string_items(item.get("design_element_ids")) if value in design_ids
        ],
        "planned_code_targets": [
            value
            for value in _string_items(item.get("planned_code_targets"))
            if _target_path(value) in task_paths
        ],
        "test_covered": item.get("test_covered"),
        "evidence_status": item.get("evidence_status"),
    }


def _project_task_trace_evidence(
    mappings: list[dict[str, object]],
    *,
    task_paths: set[str],
    test_paths: set[str],
) -> dict[str, object]:
    """Hoist byte-identical task evidence so requirement mappings only carry links."""

    evidence_by_value: dict[str, dict[str, object]] = {}
    task_test_files: set[str] = set()
    for item in mappings:
        for evidence in _dict_items(item.get("code_evidence")):
            if str(evidence.get("source_file") or "").replace("\\", "/") not in task_paths:
                continue
            key = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            evidence_by_value.setdefault(key, evidence)
        task_test_files.update(
            value.replace("\\", "/")
            for value in _string_items(item.get("test_files"))
            if value.replace("\\", "/") in test_paths
        )
    return {
        "code_evidence": [evidence_by_value[key] for key in sorted(evidence_by_value)],
        "test_files": sorted(task_test_files),
        "join_rule": (
            "Join static_trace_mappings.planned_code_targets to code_evidence.planned_code_target "
            "by exact string equality. test_files is the task-wide executed test inventory."
        ),
    }


def _code_validation_task_prompt(
    *,
    task: dict[str, object],
    code_snippets: list[dict[str, object]],
    trace_mappings: list[dict[str, object]],
    verification_evidence: dict[str, object],
    symbol_evidence: dict[str, object],
    scenario_results: list[dict[str, object]],
    approved_closure_changes: dict[str, object] | None = None,
) -> str:
    is_boundary = task.get("task_kind") == "boundary"
    authority_claim_ids = _string_items(task.get("authority_claim_ids"))
    design_ids = _string_items(task.get("design_ids"))
    context_only_design_ids = _string_items(task.get("context_only_design_ids"))
    owned_boundary_contract_ids = _string_items(task.get("owned_boundary_contract_ids"))
    requirement_refs = set(_string_items(task.get("requirement_refs")))
    task_paths = _task_paths(task)
    code_paths = {
        str(item.get("path") or "").strip().replace("\\", "/")
        for item in _dict_items(task.get("code_targets"))
        if str(item.get("path") or "").strip()
    }
    target_symbols_by_path: dict[str, set[str]] = {}
    for item in _dict_items(task.get("code_targets")):
        path = str(item.get("path") or "").strip().replace("\\", "/")
        symbol = str(item.get("symbol") or "").strip()
        if path and symbol:
            target_symbols_by_path.setdefault(path, set()).add(symbol)
    test_paths = set(_string_items(task.get("test_paths")))
    selected_mappings = [
        item
        for item in trace_mappings
        if str(item.get("requirement_ref") or "") in requirement_refs
        or (
            task.get("task_kind") == "boundary"
            and any(
                _target_path(value) in task_paths
                for value in _string_items(item.get("planned_code_targets"))
            )
        )
    ]
    projected_mappings = [
        _project_task_trace_mapping(
            item,
            design_ids=set(design_ids),
            task_paths=task_paths,
        )
        for item in selected_mappings
    ]
    scenario_claim_ownership = [
        {
            "task_id": item.get("task_id"),
            "scenario_ids": item.get("scenario_ids"),
            "scenario_owned_claim_ids": item.get("reviewed_claim_ids"),
        }
        for item in scenario_results
        if item.get("task_kind") == "scenario"
    ]
    scenario_blocker_evidence_ownership = [
        {
            "owner_task_id": result.get("task_id"),
            "scenario_ids": finding.get("scenario_ids") or result.get("scenario_ids") or [],
            "claim_ids": finding.get("claim_ids") or [],
            "unit_ids": finding.get("unit_ids") or [],
            "design_refs": finding.get("design_refs") or [],
            "code_refs": finding.get("code_refs") or [],
            "code_paths": sorted(
                {
                    _target_path(value)
                    for value in _string_items(finding.get("code_refs"))
                    if _target_path(value)
                }
            ),
            "test_paths": sorted(
                {
                    _target_path(value)
                    for value in _string_items(finding.get("test_refs"))
                    if _target_path(value)
                }
            ),
            "evidence_ref": finding.get("evidence_ref"),
            "evidence_quote": finding.get("evidence_quote"),
        }
        for result in scenario_results
        if result.get("task_kind") == "scenario"
        for finding in _dict_items(result.get("blocking_findings"))
    ]
    unit_scenarios = {
        str(unit.get("unit_id") or "").strip(): str(unit.get("scenario_id") or "GLOBAL").strip()
        or "GLOBAL"
        for unit in _dict_items(task.get("units"))
        if str(unit.get("unit_id") or "").strip()
    }
    design_scenarios = {
        str(contract.get("id") or "").strip(): str(contract.get("scenario_id") or "GLOBAL").strip()
        or "GLOBAL"
        for contract in _dict_items(task.get("approved_design_contracts"))
        if str(contract.get("id") or "").strip()
    }
    claim_review_matrix = []
    for claim in _dict_items(task.get("claims")):
        claim_kind = str(claim.get("claim_kind") or "").strip()
        if claim_kind == "cross_scenario_dependency":
            review_mode = "cross_scenario_dependency_interaction"
        elif claim_kind == "shared_code_target":
            review_mode = "same_target_cross_scenario_compatibility"
        elif task.get("task_kind") == "boundary":
            review_mode = "cross_scenario_requirement"
        else:
            review_mode = "scenario_requirement"
        claim_unit_ids = _string_items(claim.get("unit_ids"))
        claim_design_ids = _string_items(claim.get("design_element_ids"))
        claim_review_matrix.append(
            {
                "claim_id": claim.get("claim_id"),
                "review_mode": review_mode,
                "unit_ids": claim_unit_ids,
                "scenario_ids": sorted(
                    (
                        {unit_scenarios[value] for value in claim_unit_ids if value in unit_scenarios}
                        if claim_unit_ids
                        else {
                            design_scenarios[value]
                            for value in claim_design_ids
                            if value in design_scenarios
                        }
                    )
                ),
            }
        )
    ownership_exclusions = {
        "policy": (
            "Scenario-owned claims and Design field paths are not reportable in this task. "
            "A boundary finding must follow its claim_review_matrix review_mode; do not reopen "
            "a defect whose producer and consumer are owned by one scenario."
        ),
        "scenario_claim_ownership": (
            scenario_claim_ownership if task.get("task_kind") == "boundary" else []
        ),
        "scenario_blocker_evidence_ownership": (
            scenario_blocker_evidence_ownership if task.get("task_kind") == "boundary" else []
        ),
        "scenario_owned_design_sections": task.get("scenario_owned_design_sections") or [],
    }
    payload = {
        "prompt_schema_version": (
            "code_validation_task_prompt_v6" if is_boundary else "code_validation_task_prompt_v4"
        ),
        "task": "Scenario + Boundary Code Validation v1",
        "task_contract": {
            "task_id": task.get("task_id"),
            "task_kind": task.get("task_kind"),
            "scenario_ids": task.get("scenario_ids"),
            "unit_ids": task.get("unit_ids"),
            "authority_claim_ids": authority_claim_ids,
            "required_reviewed_design_ids": design_ids,
            "context_only_design_ids": context_only_design_ids,
            "required_reviewed_boundary_contract_ids": owned_boundary_contract_ids,
            "allowed_requirement_refs": sorted(requirement_refs),
            "allowed_code_paths": sorted(code_paths),
            "allowed_test_paths": sorted(test_paths),
            "allowed_owner_action_pairs": [
                ["developer", "developer_repair"],
                ["validator", "validator_mapping_fix"],
                ["system", "block_run"],
                ["none", "none"],
            ],
            "repair_policy": {
                "semantic_authority": ["authority_claims", "approved_design_contracts"],
                "explanatory_context": "related_design_context.contracts",
                "frozen_artifacts": ["Requirement", "Design", "Plan"],
                "implementation_scope": "task unit_ids plus allowed_code_paths and allowed_test_paths",
                "implementation_choice_owner": "developer",
                "validator_expected_change_mode": "required_outcome_only",
                "out_of_scope_or_unsatisfiable_requirement": "system/block_run",
                "approved_design_conflict": "follow the reading that satisfies the owned Requirement; record it in observations",
                "human_decision_available": False,
            },
            "review_scope": {
                "active_dimensions": [
                    "owned Requirement behavior",
                    "owned Design behavior and boundary compatibility",
                    "source and unit-test semantics provable from supplied evidence",
                    "typecheck and unit verification evidence",
                ],
                "deferred_dimensions": [
                    "rendered visual appearance",
                    "responsive layout",
                    "real-browser focus and announcement behavior",
                    "DOM event timing or interaction not provable from current evidence",
                    "end-to-end behavior not provable from current source and executed stages",
                ],
                "deferred_destination": "observations only; Browser/UI QA owns the verdict",
                "warning_definition": "fully evidenced, non-blocking issue inside active_dimensions",
                "observation_definition": "deferred-stage concern, unexecuted-stage limitation, or uncertainty",
                "stop_condition": (
                    "Assess each authority claim and owned boundary contract once; after all are classified, "
                    "return JSON without reconsidering a verdict unless new supplied evidence contradicts it."
                ),
            },
            "model_created_id_fields": ["finding_id"],
        },
        "instruction": (
            ("Apply ownership_exclusions and task_contract.review_scope first. " if is_boundary else "Apply task_contract.review_scope first. ")
            + "Review only this task's authority claims against the supplied owned contracts, current code/test "
            "evidence, and executed verification evidence. Decide semantic correctness, not JSON shape. A file or "
            "symbol existing is not enough when its behavior or test assertions do not satisfy the claim. Use "
            "related_design_context only to interpret behavior in shared code entities; do not review or create "
            "findings for its scenarios. Do not speculate about deferred dimensions."
        ),
        **(
            {
                "review_procedure": [
                    "Read ownership_exclusions first and exclude every listed local evidence fingerprint from boundary findings.",
                    "Assess each claim_review_matrix row exactly once and record one claim_assessment.",
                    "A pass is a valid result; there is no blocker quota and no hidden-defect search after every row is assessed.",
                    "When claim_assessments covers the exact authority claim set, return the JSON immediately.",
                ],
                "ownership_exclusions": ownership_exclusions,
            }
            if is_boundary
            else {}
        ),
        "expected_json_schema": {
            "task_id": "exact task_contract.task_id",
            "reviewed_claim_ids": "exact complete authority_claim_ids array",
            "reviewed_design_ids": "exact complete required_reviewed_design_ids array",
            "reviewed_boundary_contract_ids": "exact complete required_reviewed_boundary_contract_ids array",
            **(
                {
                    "claim_assessments": [
                        {
                            "claim_id": "one exact authority_claim_id; every authority claim appears exactly once",
                            "verdict": "pass | finding",
                            "finding_ids": "exact finding_ids whose claim_ids contain this claim; empty for pass",
                        }
                    ]
                }
                if is_boundary
                else {}
            ),
            "ok": "boolean; true only when findings has no blocker",
            "summary": "short string",
            "findings": [
                {
                    "finding_id": "new unique stable string; this is the only ID you may create",
                    "category": "semantic_gap | missing_behavior | missing_test_assertion | weak_trace | trace_mapping_issue | evidence_incomplete",
                    "severity": "blocker | warning",
                    "blocking": "boolean matching severity",
                    "scenario_ids": "subset of task scenario_ids",
                    "claim_ids": "non-empty subset of authority_claim_ids",
                    "unit_ids": "non-empty subset of task unit_ids",
                    "requirement_refs": "subset of task_contract.allowed_requirement_refs belonging to the selected claim_ids",
                    "design_refs": "subset of required_reviewed_design_ids",
                    "code_refs": "file or file#symbol refs under task_contract.allowed_code_paths",
                    "test_refs": "file or file#test refs under task_contract.allowed_test_paths",
                    "issue": "short explanation",
                    "expected_change": "developer blocker: required observable outcome only; validator blocker: mapping/evidence correction; system blocker: unsatisfiable or out-of-scope invariant; warning: empty string",
                    "evidence_ref": "one supplied section/id",
                    "evidence_quote": "exact text visible in this input",
                    "owner": "developer | validator | system | none",
                    "recommended_action": "developer_repair | validator_mapping_fix | block_run | none",
                }
            ],
            "observations": ["short deferred-stage or other non-blocking strings; never repair instructions"],
        },
        "authority_claims": task.get("claims"),
        "claim_review_matrix": claim_review_matrix,
        "approved_design_contracts": task.get("approved_design_contracts"),
        "deferred_review_context": {
            "stage": "browser_ui_qa",
            "policy": (
                "These typed Design sections were intentionally omitted from current semantic authority. "
                "Do not infer their content or create findings for them in this task."
            ),
            "design_sections": task.get("deferred_design_sections"),
        },
        "related_design_context": {
            "role": "explanatory_only",
            "selection_rule": (
                "Approved Design contracts from other scenarios whose nodes use the exact same "
                "runtime_binding.target as this task; only edges between those shared nodes or task-owned nodes."
            ),
            "context_only_design_ids": context_only_design_ids,
            "contracts": task.get("context_only_design_contracts"),
        },
        **(
            {
                "ownership_exclusions": {
                    "policy": (
                        "Scenario-owned claims and Design field paths are not reportable in this task. "
                        "A boundary finding must follow its claim_review_matrix review_mode; do not reopen "
                        "a defect whose producer and consumer are owned by one scenario."
                    ),
                    "scenario_claim_ownership": [],
                    "scenario_owned_design_sections": task.get("scenario_owned_design_sections") or [],
                }
            }
            if not is_boundary
            else {}
        ),
        "approved_units": task.get("units"),
        "static_trace_mappings": projected_mappings,
        "static_trace_evidence": _project_task_trace_evidence(
            selected_mappings,
            task_paths=task_paths,
            test_paths=test_paths,
        ),
        "verification_evidence": _project_verification_evidence(
            verification_evidence,
            task_paths=task_paths,
            test_paths=test_paths,
        ),
        "symbol_evidence": _project_symbol_evidence(
            symbol_evidence,
            paths=task_paths,
            symbols_by_path=target_symbols_by_path,
        ),
        "code_snippets": code_snippets,
        "validator_rules": [
            "Return every active-scope finding in this task in one response; do not stop after the first blocker.",
            *(
                [
                    "claim_assessments is the completion checklist: emit exactly one row per authority claim, link every finding to each claim it cites, and stop once the exact claim set is assessed."
                ]
                if is_boundary
                else []
            ),
            "Use only exact task_contract IDs and paths; create only finding_id. Each requirement_ref must belong to a claim_id selected by the same finding.",
            "authority_claims and approved_design_contracts are this task's semantic authority. approved_units are a compact frozen implementation scope index, not new business authority; a Plan-only expectation unsupported by Requirement or approved Design cannot block.",
            "related_design_context is approved but explanatory-only context for exact shared runtime targets. Check it before calling shared-code behavior invented or unsupported. Never review its scenarios, add its IDs to reviewed_design_ids or design_refs, or create findings for it. Its presence does not excuse code that breaks an owned claim or owned contract, except under the approved-Design-conflict rule.",
            "Approved Design contracts froze before implementation and can contradict each other. When an owned contract and another approved contract (owned or in related_design_context) require incompatible behavior from the same code, do not block the run and do not require code to break one approved contract to satisfy the other. Accept the current implementation when it follows one of the conflicting contracts and still satisfies the owned Requirement claims; otherwise report a developer finding for the reading that satisfies them. Record each such conflict in observations with both Design IDs and the reading you applied.",
            "Classify each concern with task_contract.review_scope before judging it. Anything that needs deferred browser, DOM, visual, responsive, focus or announcement evidence goes only to observations; do not speculate toward a finding.",
            "For an in-scope Requirement/Design gap, use developer/developer_repair and state only the observable outcome and evidence. Developer owns implementation choice. Do not edit frozen artifacts or prescribe a field, header, credential strategy, protocol, component, or topology unless Requirement/Design explicitly does.",
            "Human decision is unavailable after Requirement approval. If no reading of the frozen authority can satisfy the owned Requirement claims, or the outcome cannot fit task units and allowed paths, use system/block_run and state the invariant; never assign out-of-scope work.",
            "Every required_reviewed_boundary_contract_id is owned here. A same-scenario boundary_contract stays with the scenario task. Compare actual producer and consumer; separate endpoint tests are not integration proof, and an unadapted incompatibility that breaks the approved runtime interaction is a blocker.",
            "task_kind=boundary owns only cross-scenario dependencies, shared-target compatibility and cross-scenario claims; it never re-reviews a scenario-local claim or boundary_contract.",
            "Judge executable evidence only within verification_evidence stages. Absence of a deferred stage is not a defect and never creates a finding in this task.",
            "static_trace_mappings are candidate links, not semantic proof. Join their planned_code_targets to static_trace_evidence.code_evidence by exact planned_code_target; static_trace_evidence.test_files is task-wide inventory. Use validator/validator_mapping_fix only for a supplied trace/evidence projection defect, never a product behavior defect.",
            *(
                [
                    "Use claim_review_matrix as the finding boundary. cross_scenario_dependency_interaction compares only the listed dependency units; same_target_cross_scenario_compatibility compares only the exact shared target across its listed scenarios; cross_scenario_requirement reviews only the owned requirement across scenarios.",
                    "ownership_exclusions is scope metadata, not semantic authority. Never reopen its scenario-owned claims or Design sections. scenario_blocker_evidence_ownership contains only structural evidence fingerprints already owned by local blockers; do not restate or relabel the same producer/consumer defect under a cross-scenario claim. A boundary finding must identify a matrix-owned interaction beyond every local fingerprint. A defect whose producer and consumer belong to one scenario remains local even when their files are shared by other scenarios.",
                ]
                if task.get("task_kind") == "boundary"
                else []
            ),
            "Do not reinterpret a deterministic Harness rule or invent a new requirement, field name, component, or topology.",
            "A warning finding requires complete visible evidence inside active_dimensions; use none/none and an empty expected_change. Put deferred-stage concerns and incomplete-evidence uncertainty in observations instead. Blockers must use developer, validator or system ownership as defined above, provide the matching expected_change kind, and cite exact visible evidence.",
            "Use only one of task_contract.allowed_owner_action_pairs for every finding.",
            "Set ok=false if and only if at least one finding has severity=blocker.",
            "Use the review_scope stop_condition: assess each owned claim and boundary once, then return JSON without re-litigating settled judgments.",
        ],
        "egress_confirmation": "Project owner approved sending these bounded source/test excerpts to the LLM provider.",
    }
    closure_changes = _dict_value(approved_closure_changes)
    touched = sorted(
        set(_string_items(closure_changes.get("changed_paths")))
        & {str(item.get("path") or "").replace("\\", "/") for item in code_snippets}
    )
    if touched:
        # Context, not a gate: the owner approved these closure changes after the Design froze.
        payload["approved_closure_changes"] = {
            **{key: value for key, value in closure_changes.items() if key != "changed_paths"},
            "changed_paths_in_this_task": touched,
        }
    return json.dumps(payload, ensure_ascii=False, indent=0)


def _validate_code_validation_task_output(
    *,
    output: dict[str, object],
    task: dict[str, object],
    prompt: str,
) -> None:
    task_id = str(task.get("task_id") or "")
    if output.get("task_id") != task_id:
        raise ValueError(f"task_id must be exactly {task_id}.")
    expected_claims = set(_string_items(task.get("authority_claim_ids")))
    reviewed_claims = _exact_string_array(output.get("reviewed_claim_ids"), "reviewed_claim_ids")
    if set(reviewed_claims) != expected_claims or len(reviewed_claims) != len(expected_claims):
        raise ValueError("reviewed_claim_ids must be the exact complete authority claim set.")
    expected_design = set(_string_items(task.get("design_ids")))
    reviewed_design = _exact_string_array(output.get("reviewed_design_ids"), "reviewed_design_ids")
    if set(reviewed_design) != expected_design or len(reviewed_design) != len(expected_design):
        raise ValueError("reviewed_design_ids must be the exact complete task design ID set.")
    expected_boundary_contracts = set(_string_items(task.get("owned_boundary_contract_ids")))
    reviewed_boundary_contracts = _exact_string_array(
        output.get("reviewed_boundary_contract_ids"),
        "reviewed_boundary_contract_ids",
    )
    if (
        set(reviewed_boundary_contracts) != expected_boundary_contracts
        or len(reviewed_boundary_contracts) != len(expected_boundary_contracts)
    ):
        raise ValueError(
            "reviewed_boundary_contract_ids must be the exact complete owned boundary contract ID set."
        )
    if not isinstance(output.get("ok"), bool) or not isinstance(output.get("summary"), str):
        raise ValueError("ok must be boolean and summary must be a string.")
    findings = output.get("findings")
    claim_assessments = output.get("claim_assessments")
    observations = output.get("observations")
    requires_claim_assessments = task.get("task_kind") == "boundary"
    if not isinstance(findings, list) or not isinstance(observations, list) or not all(
        isinstance(item, str) for item in observations
    ):
        raise ValueError("findings must be an array and observations must be an array of strings.")
    if requires_claim_assessments and not isinstance(claim_assessments, list):
        raise ValueError("Boundary claim_assessments must be an array.")

    assessments_by_claim: dict[str, dict[str, object]] = {}
    for raw_assessment in claim_assessments if isinstance(claim_assessments, list) else []:
        if not isinstance(raw_assessment, dict):
            raise ValueError("Every claim_assessment must be an object.")
        claim_id = str(raw_assessment.get("claim_id") or "").strip()
        if claim_id not in expected_claims or claim_id in assessments_by_claim:
            raise ValueError("claim_assessments must contain each authority claim exactly once.")
        verdict = str(raw_assessment.get("verdict") or "").strip()
        if verdict not in {"pass", "finding"}:
            raise ValueError(f"Claim assessment {claim_id} has an invalid verdict.")
        _exact_string_array(raw_assessment.get("finding_ids"), f"{claim_id}.finding_ids")
        assessments_by_claim[claim_id] = raw_assessment
    if requires_claim_assessments and set(assessments_by_claim) != expected_claims:
        raise ValueError("claim_assessments must contain the exact complete authority claim set.")

    allowed_scenarios = set(_string_items(task.get("scenario_ids")))
    allowed_units = set(_string_items(task.get("unit_ids")))
    allowed_requirements = set(_string_items(task.get("requirement_refs")))
    requirements_by_claim = {
        str(claim.get("claim_id") or "").strip(): {
            value
            for value in [
                str(claim.get("requirement_ref") or "").strip(),
                *_string_items(claim.get("requirement_refs")),
            ]
            if value
        }
        for claim in _dict_items(task.get("claims"))
        if str(claim.get("claim_id") or "").strip()
    }
    allowed_code_paths = {
        str(item.get("path") or "").strip().replace("\\", "/")
        for item in _dict_items(task.get("code_targets"))
        if str(item.get("path") or "").strip()
    }
    allowed_test_paths = set(_string_items(task.get("test_paths")))
    allowed_categories = {
        "semantic_gap",
        "missing_behavior",
        "missing_test_assertion",
        "weak_trace",
        "trace_mapping_issue",
        "evidence_incomplete",
    }
    allowed_owner_actions = {
        ("developer", "developer_repair"),
        ("validator", "validator_mapping_fix"),
        ("system", "block_run"),
        ("none", "none"),
    }
    finding_ids: set[str] = set()
    finding_claims: dict[str, set[str]] = {}
    blocking_count = 0
    grounding_text = _task_prompt_grounding_text(prompt)
    for raw in findings:
        if not isinstance(raw, dict):
            raise ValueError("Every finding must be an object.")
        finding_id = str(raw.get("finding_id") or "").strip()
        if not finding_id or finding_id in finding_ids:
            raise ValueError("Every finding_id must be non-empty and unique within the task.")
        finding_ids.add(finding_id)
        category = str(raw.get("category") or "").strip()
        severity = str(raw.get("severity") or "").strip()
        blocking = raw.get("blocking")
        if category not in allowed_categories or severity not in {"blocker", "warning"}:
            raise ValueError(f"Finding {finding_id} has an invalid category or severity.")
        if not isinstance(blocking, bool) or blocking != (severity == "blocker"):
            raise ValueError(f"Finding {finding_id} blocking must match severity.")
        blocking_count += int(blocking)
        claim_ids = _exact_string_array(raw.get("claim_ids"), f"{finding_id}.claim_ids")
        finding_claims[finding_id] = set(claim_ids)
        unit_ids = _exact_string_array(raw.get("unit_ids"), f"{finding_id}.unit_ids")
        scenario_ids = _exact_string_array(raw.get("scenario_ids"), f"{finding_id}.scenario_ids")
        requirement_refs = _exact_string_array(raw.get("requirement_refs"), f"{finding_id}.requirement_refs")
        design_refs = _exact_string_array(raw.get("design_refs"), f"{finding_id}.design_refs")
        code_refs = _exact_string_array(raw.get("code_refs"), f"{finding_id}.code_refs")
        test_refs = _exact_string_array(raw.get("test_refs"), f"{finding_id}.test_refs")
        if not claim_ids or not set(claim_ids).issubset(expected_claims):
            raise ValueError(f"Finding {finding_id} must cite task-owned claim_ids.")
        if not unit_ids or not set(unit_ids).issubset(allowed_units):
            raise ValueError(f"Finding {finding_id} must cite task-owned unit_ids.")
        if not set(scenario_ids).issubset(allowed_scenarios):
            raise ValueError(f"Finding {finding_id} has an out-of-task scenario_id.")
        if not set(requirement_refs).issubset(allowed_requirements):
            raise ValueError(f"Finding {finding_id} has an out-of-task requirement_ref.")
        cited_claim_requirements = {
            requirement_ref
            for claim_id in claim_ids
            for requirement_ref in requirements_by_claim.get(claim_id, set())
        }
        unrelated_requirement_refs = sorted(set(requirement_refs) - cited_claim_requirements)
        if unrelated_requirement_refs:
            raise ValueError(
                f"Finding {finding_id} requirement_refs must belong to its cited claim_ids; "
                "refs without a selected matching claim: " + ", ".join(unrelated_requirement_refs) + "."
            )
        if not set(design_refs).issubset(expected_design):
            raise ValueError(f"Finding {finding_id} has an out-of-task design_ref.")
        if any(_target_path(value) not in allowed_code_paths for value in code_refs):
            raise ValueError(f"Finding {finding_id} has an out-of-task code_ref.")
        if any(_target_path(value) not in allowed_test_paths for value in test_refs):
            raise ValueError(f"Finding {finding_id} has an out-of-task test_ref.")
        issue = str(raw.get("issue") or "").strip()
        expected_change = str(raw.get("expected_change") or "").strip()
        evidence_ref = str(raw.get("evidence_ref") or "").strip()
        evidence_quote = str(raw.get("evidence_quote") or "").strip()
        if (
            not issue
            or not evidence_ref
            or not evidence_quote
            or _normalized_evidence_text(evidence_quote) not in grounding_text
        ):
            raise ValueError(f"Finding {finding_id} must cite exact evidence visible in the task input.")
        if blocking and not expected_change:
            raise ValueError(f"Blocking finding {finding_id} requires expected_change.")
        owner_action = (
            str(raw.get("owner") or "").strip(),
            str(raw.get("recommended_action") or "").strip(),
        )
        if owner_action not in allowed_owner_actions:
            raise ValueError(f"Finding {finding_id} has an invalid owner/action pair.")
        if severity == "warning" and owner_action != ("none", "none"):
            raise ValueError(f"Warning {finding_id} must use none/none.")
        if severity == "warning" and expected_change:
            raise ValueError(f"Warning {finding_id} expected_change must be empty.")
        if severity == "blocker" and owner_action == ("none", "none"):
            raise ValueError(f"Blocking finding {finding_id} must have an actionable owner/action pair.")
    if output.get("ok") is not (blocking_count == 0):
        raise ValueError("ok must be true if and only if the task has no blocking findings.")
    for claim_id, assessment in assessments_by_claim.items():
        expected_finding_ids = {
            finding_id for finding_id, claim_ids in finding_claims.items() if claim_id in claim_ids
        }
        assessed_finding_ids = set(
            _exact_string_array(assessment.get("finding_ids"), f"{claim_id}.finding_ids")
        )
        verdict = str(assessment.get("verdict") or "").strip()
        if assessed_finding_ids != expected_finding_ids or (verdict == "finding") != bool(
            expected_finding_ids
        ):
            raise ValueError(
                f"Claim assessment {claim_id} verdict and finding_ids must match findings that cite it."
            )


def _task_findings(
    *,
    review: dict[str, object],
    task: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    blockers: list[dict[str, object]] = []
    observations: list[dict[str, object]] = [
        {
            "type": "llm_observation",
            "blocking": False,
            "owner": "none",
            "recommended_action": "none",
            "message": value,
            "task_id": task.get("task_id"),
        }
        for value in _string_items(review.get("observations"))
    ]
    for raw in _dict_items(review.get("findings")):
        owner = str(raw.get("owner") or "").strip()
        category = str(raw.get("category") or "").strip()
        link_type = (
            "frozen_contract_conflict"
            if owner == "system"
            else category
            if category in NON_REPAIRABLE_LLM_LINK_TYPES
            else "llm_semantic_gap"
        )
        item = {
            **raw,
            "type": link_type,
            "llm_type": category,
            "task_id": task.get("task_id"),
            "message": raw.get("issue"),
            "claim_id": next(iter(_string_items(raw.get("claim_ids"))), ""),
            "requirement_ref": next(iter(_string_items(raw.get("requirement_refs"))), ""),
            "design_element_id": next(iter(_string_items(raw.get("design_refs"))), ""),
            "code_ref": next(iter(_string_items(raw.get("code_refs"))), ""),
        }
        if raw.get("blocking") is True:
            blockers.append(item)
        else:
            observations.append(item)
    return blockers, observations


def _task_code_snippets(
    *,
    target_path: Path,
    task: dict[str, object],
    ast_evidence: TypeScriptAstEvidence,
    fallback_test_paths: list[str],
) -> list[dict[str, object]]:
    symbols_by_path: dict[str, set[str]] = {}
    source_paths: list[str] = []
    for target in _dict_items(task.get("code_targets")):
        path = str(target.get("path") or "").strip().replace("\\", "/")
        symbol = str(target.get("symbol") or "").strip()
        if not path:
            continue
        if path not in source_paths:
            source_paths.append(path)
        if symbol:
            symbols_by_path.setdefault(path, set()).add(symbol)
    test_paths = _string_items(task.get("test_paths"))
    if not test_paths and task.get("scenario_ids") == ["GLOBAL"]:
        test_paths = fallback_test_paths
    test_paths = [path for path in test_paths if path not in source_paths]

    snippets: list[dict[str, object]] = []
    for paths, total_limit, file_limit in (
        (source_paths, MAX_TASK_SOURCE_SNIPPET_TOTAL_CHARS, MAX_TASK_SOURCE_SNIPPET_FILE_CHARS),
        (test_paths, MAX_TASK_TEST_SNIPPET_TOTAL_CHARS, MAX_TASK_TEST_SNIPPET_FILE_CHARS),
    ):
        remaining = total_limit
        for path in paths:
            file_path = target_path / path
            if remaining <= 0 or not file_path.is_file():
                continue
            text = file_path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            ranges = _task_symbol_dependency_ranges(
                ast_evidence=ast_evidence,
                path=path,
                symbols=symbols_by_path.get(path, set()),
                lines=lines,
            )
            selected_ranges = _merge_prioritized_line_ranges(
                (
                    max(1, start - TASK_SYMBOL_CONTEXT_LINES),
                    min(len(lines), end + TASK_SYMBOL_CONTEXT_LINES),
                )
                for start, end in ranges
            )
            requested_ranges = selected_ranges or ([(1, len(lines))] if lines else [])
            limit = min(file_limit, remaining)
            bounded, included_ranges = _render_bounded_line_ranges(
                lines=lines,
                ranges=requested_ranges,
                limit=limit,
            )
            snippets.append(
                {
                    "path": path,
                    "content": bounded,
                    "chars_total": len(text),
                    "chars_included": sum(
                        len("".join(lines[start - 1 : end])) for start, end in included_ranges
                    ),
                    "content_truncated": (
                        sum(end - start + 1 for start, end in requested_ranges) < len(lines)
                        or included_ranges != requested_ranges
                    ),
                    "line_ranges": [
                        {"start_line": start, "end_line": end}
                        for start, end in included_ranges
                    ],
                }
            )
            remaining -= len(bounded)
    return snippets


def _task_symbol_dependency_ranges(
    *,
    ast_evidence: TypeScriptAstEvidence,
    path: str,
    symbols: set[str],
    lines: list[str],
) -> list[tuple[int, int]]:
    """Include local declarations referenced by the planned target, in dependency order."""

    primary = list(ast_evidence.ranges_for(path, symbols=symbols))
    if not primary or not symbols:
        return primary
    candidates = [
        symbol
        for file in ast_evidence.files
        if file.get("path") == path
        for symbol in _dict_items(file.get("symbols"))
    ]
    selected = set(primary)
    ordered = list(primary)
    frontier = list(primary)
    while frontier:
        referenced_text = "\n".join("".join(lines[start - 1 : end]) for start, end in frontier)
        discovered: list[tuple[int, int]] = []
        for symbol in candidates:
            start = int(symbol.get("start_line") or 0)
            end = int(symbol.get("end_line") or 0)
            candidate = (start, end)
            name = str(symbol.get("name") or "").strip()
            if start <= 0 or end < start or candidate in selected or not name:
                continue
            if re.search(rf"\b{re.escape(name)}\b", referenced_text):
                selected.add(candidate)
                discovered.append(candidate)
        ordered.extend(discovered)
        frontier = discovered
    return ordered


def _merge_prioritized_line_ranges(
    ranges: Any,
) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in ranges:
        overlaps = [
            index
            for index, existing in enumerate(merged)
            if start <= existing[1] + 1 and end >= existing[0] - 1
        ]
        if not overlaps:
            merged.append([start, end])
            continue
        first = overlaps[0]
        merged[first] = [
            min(start, *(merged[index][0] for index in overlaps)),
            max(end, *(merged[index][1] for index in overlaps)),
        ]
        for index in reversed(overlaps[1:]):
            del merged[index]
    return [(start, end) for start, end in merged]


def _render_bounded_line_ranges(
    *,
    lines: list[str],
    ranges: list[tuple[int, int]],
    limit: int,
) -> tuple[str, list[tuple[int, int]]]:
    parts: list[str] = []
    included: list[tuple[int, int]] = []
    used = 0
    for start, end in ranges:
        header = f"/* lines {start}-{end} */\n"
        segment: list[str] = []
        segment_used = len(header) + (1 if parts else 0)
        actual_end = start - 1
        for line_number in range(start, end + 1):
            line = lines[line_number - 1]
            if used + segment_used + len(line) > limit:
                break
            segment.append(line)
            segment_used += len(line)
            actual_end = line_number
        if actual_end < start:
            break
        parts.append(header + "".join(segment))
        included.append((start, actual_end))
        used += segment_used
        if actual_end < end:
            break
    return "\n".join(parts), included


def _merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _project_symbol_evidence(
    symbol_evidence: dict[str, object],
    *,
    paths: set[str],
    symbols_by_path: dict[str, set[str]],
) -> dict[str, object]:
    return {
        "version": symbol_evidence.get("version"),
        "extractor_id": symbol_evidence.get("extractor_id"),
        "profile": symbol_evidence.get("profile"),
        "files": [
            {
                **item,
                "symbols": [
                    symbol
                    for symbol in _dict_items(item.get("symbols"))
                    if _symbol_matches_targets(
                        symbol,
                        targets=symbols_by_path.get(str(item.get("path") or ""), set()),
                    )
                ],
            }
            for item in _dict_items(symbol_evidence.get("files"))
            if str(item.get("path") or "") in paths
            and symbols_by_path.get(str(item.get("path") or ""))
        ],
    }


def _symbol_matches_targets(symbol: dict[str, object], *, targets: set[str]) -> bool:
    name = str(symbol.get("name") or "")
    qualified = str(symbol.get("qualified_name") or "")
    return any(
        target == name or target == qualified or qualified.endswith(f".{target}")
        for target in targets
    )


def _task_paths(task: dict[str, object]) -> set[str]:
    return {
        path
        for path in [
            *[
                str(item.get("path") or "").strip().replace("\\", "/")
                for item in _dict_items(task.get("code_targets"))
            ],
            *_string_items(task.get("test_paths")),
        ]
        if path
    }


def _exact_string_array(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() == item and item for item in value):
        raise ValueError(f"{field} must be an array of non-empty exact strings.")
    if len(value) != len(set(value)):
        raise ValueError(f"{field} must not contain duplicates.")
    return list(value)


def _task_review_summary(
    *,
    task_results: list[dict[str, object]],
    blockers: list[dict[str, object]],
    inconclusive: bool,
    deterministic_blocked: bool,
) -> str:
    if deterministic_blocked:
        return "Deterministic preflight found blockers; semantic calls were skipped."
    if inconclusive:
        return "At least one semantic task was inconclusive; no repair is authorized."
    if blockers:
        return f"Scenario + Boundary review found {len(blockers)} blocking finding(s)."
    return f"Scenario + Boundary review passed {len(task_results)} task(s)."


def _task_prompt_grounding_text(prompt: str) -> str:
    try:
        payload = json.loads(prompt)
    except json.JSONDecodeError:
        return _normalized_evidence_text(prompt)
    values: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, dict):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(payload)
    return _normalized_evidence_text("\n".join(values))


def _target_path(value: str) -> str:
    return str(value or "").split("#", 1)[0].strip().replace("\\", "/")


def _code_snippets(
    *,
    target_path: Path,
    mappings: list[dict[str, object]],
    test_paths: list[str],
) -> list[dict[str, object]]:
    """Lấy source/test snippet để LLM review được nội dung code thật."""

    paths = _snippet_paths(mappings=mappings, test_paths=test_paths)
    snippets: list[dict[str, object]] = []
    total_chars = 0
    for rel_path in paths:
        if total_chars >= MAX_SNIPPET_TOTAL_CHARS:
            break
        file_path = target_path / rel_path
        if not file_path.is_file():
            continue
        content = file_path.read_text(encoding="utf-8", errors="replace")
        remaining = MAX_SNIPPET_TOTAL_CHARS - total_chars
        limit = min(MAX_SNIPPET_FILE_CHARS, remaining)
        snippet = content[:limit]
        total_chars += len(snippet)
        snippets.append(
            {
                "path": rel_path,
                "role": "test" if rel_path in test_paths else "source",
                "content": snippet,
                "content_truncated": len(snippet) < len(content),
                "chars_total": len(content),
                "chars_sent": len(snippet),
            }
        )
    return snippets


def _snippet_paths(*, mappings: list[dict[str, object]], test_paths: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    for item in mappings:
        evidence = _dict_items(item.get("code_evidence")) or [item]
        for value in evidence:
            path = str(value.get("source_file") or "").strip().replace("\\", "/")
            if path:
                counts[path] = counts.get(path, 0) + 1
    for path in test_paths:
        counts[path] = max(counts.get(path, 0), 1)
    return sorted(counts, key=lambda path: _snippet_priority(path, counts.get(path, 0), test_paths))


def _snippet_priority(path: str, mapping_count: int, test_paths: list[str]) -> tuple[int, int, str]:
    name = Path(path).name.lower()
    if "controller" in name:
        group = 0
    elif path in test_paths:
        group = 1
    elif "service" in name or "repository" in name:
        group = 2
    else:
        group = 3
    return (group, -mapping_count, path)
