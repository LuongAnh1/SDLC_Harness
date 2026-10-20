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
from typing import Any

from ...core.errors import HarnessError
from ...llm.client import LlmClient
from ...llm.json_utils import parse_first_json_object
from ...profiles import CodeTracePolicy
from ...storage.runtime import utc_now_iso
from .code_contract import (
    aggregate_semantic_trials,
    build_finding_ledger,
    build_validation_claims,
    deterministic_contract_findings,
    prepare_semantic_trial,
    snapshot_fingerprint,
    trial_blocker_keys,
)
from .typescript_ast import TypeScriptAstEvidence, TypeScriptAstExtractor

MAX_SNIPPET_TOTAL_CHARS = 60000
MAX_SNIPPET_FILE_CHARS = 20000
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
    disputed_findings: list[dict[str, object]] = field(default_factory=list)
    rejected_findings: list[dict[str, object]] = field(default_factory=list)
    semantic_consensus: dict[str, object] = field(default_factory=dict)
    verification_evidence: dict[str, object] = field(default_factory=dict)
    symbol_evidence: dict[str, object] = field(default_factory=dict)
    snapshot_fingerprint: str = ""
    ledger: dict[str, object] = field(default_factory=dict)
    inconclusive: bool = False

    def to_dict(self) -> dict[str, object]:
        contract = self.requirement_contract or {"version": "v1", "revision": 0}
        mode = "hybrid_rule_llm_code_trace_v3" if contract.get("version") == "v2" else "hybrid_rule_llm_code_trace_v1"
        return {
            "version": "requirement_design_code_trace_v1",
            "mode": mode,
            "requirement_contract": contract,
            "created_at": utc_now_iso(),
            "ok": self.ok,
            "summary": self.summary,
            "stats": self.stats,
            "mappings": self.mappings,
            "missing_links": self.missing_links,
            "deterministic_findings": self.deterministic_findings,
            "disputed_findings": self.disputed_findings,
            "rejected_findings": self.rejected_findings,
            "semantic_consensus": self.semantic_consensus,
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
        mode = "hybrid_rule_llm_code_trace_v3" if contract.get("version") == "v2" else "hybrid_rule_llm_code_trace_v1"
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
            "mode": mode,
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
            "consensus_blocking_count": len(self.semantic_consensus.get("stable_blockers", [])),
            "disputed_count": len(self.disputed_findings),
            "semantic_consensus": self.semantic_consensus,
            "symbol_evidence": symbol_evidence_summary,
            "inconclusive": self.inconclusive,
        }


class CodeTraceValidator:
    """Kiểm approved design mappings đã đi tới code/test bằng rule và LLM."""

    def __init__(
        self,
        llm_client: LlmClient,
        *,
        validation_trials: int = 1,
        llm_call_budget: int = 2,
        symbol_extractor: TypeScriptAstExtractor | None = None,
    ):
        self.llm_client = llm_client
        self.validation_trials = max(1, int(validation_trials))
        self.llm_call_budget = max(1, int(llm_call_budget))
        self.symbol_extractor = symbol_extractor or TypeScriptAstExtractor()

    def validate(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        approved_context: dict[str, object] | None = None,
        verification_evidence: dict[str, object] | None = None,
        previous_ledger: dict[str, object] | None = None,
        code_trace_policy: CodeTracePolicy | None = None,
    ) -> CodeTraceResult:
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
        ast_evidence: TypeScriptAstEvidence | None = None
        symbol_evidence: dict[str, object] = {}
        if code_trace_policy is not None:
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
                source_path = _normalized_source_file(
                    raw_source_path,
                    item,
                    legacy=code_trace_policy is None,
                )
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
                symbol = _normalized_symbol(raw_symbol, item, legacy=code_trace_policy is None)
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
                        "symbol_evidence_source": (
                            ast_evidence.extractor_id if ast_evidence is not None else "legacy_text_search"
                        ),
                        "requires_direct_symbol": bool(symbol),
                        "trace_policy": _trace_policy(raw_symbol, item),
                        "mapping_note": _mapping_note(raw_symbol, item),
                        "test_files": existing_test_paths,
                        "test_covered": bool(existing_test_paths),
                    }
                )

        deterministic_findings = (
            deterministic_contract_findings(
                target_path=target_path,
                work_packet=work_packet,
                approved_context=approved_context,
            )
            if code_trace_policy is None
            else []
        )
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
        valid_trials: list[dict[str, object]] = []
        invalid_trials: list[dict[str, object]] = []
        rejected_findings: list[dict[str, object]] = []
        observations: list[dict[str, object]] = []
        calls_used = 0
        target_valid_trials = 1 if self.validation_trials == 1 else 2
        while len(valid_trials) < target_valid_trials and calls_used < self.llm_call_budget:
            trial_id = f"trial-{len(valid_trials) + 1}"
            max_attempts = min(2, self.llm_call_budget - calls_used)
            try:
                review = self._review_with_llm(
                    target_path=target_path,
                    work_packet=work_packet,
                    trace_mappings=trace_mappings,
                    static_missing_links=static_missing_links,
                    test_paths=existing_test_paths,
                    approved_context=approved_context,
                    validation_claims=validation_claims,
                    verification_evidence=verification_evidence,
                    symbol_evidence=symbol_evidence,
                    max_attempts=max_attempts,
                )
            except HarnessError as exc:
                calls_used += max_attempts
                invalid_trials.append({"attempt_id": f"attempt-{calls_used}", "message": exc.message})
                continue
            metadata = _dict_value(review.get("llm_metadata"))
            calls_used += max(1, int(metadata.get("selected_attempt") or 1))
            normalized, trial_observations = _normalize_llm_findings(
                review,
                approved_context=approved_context,
            )
            accepted, trial_observations, rejected = prepare_semantic_trial(
                findings=normalized,
                observations=trial_observations,
                claims=validation_claims,
                verification_evidence=verification_evidence,
                trial_id=trial_id,
            )
            rejected_findings.extend(rejected)
            observations.extend(trial_observations)
            valid_trials.append(
                {
                    "trial_id": trial_id,
                    "reported_ok": review.get("ok") is True,
                    "confidence": review.get("confidence"),
                    "summary": review.get("summary"),
                    "blocking_findings": accepted,
                    "observations": trial_observations,
                    "review_context": review.get("review_context"),
                    "llm_metadata": metadata,
                }
            )
            if len(valid_trials) == 2 and self.validation_trials >= 3:
                if trial_blocker_keys(valid_trials[0]) != trial_blocker_keys(valid_trials[1]):
                    target_valid_trials = 3

        disagreement_needs_third = (
            len(valid_trials) >= 2
            and trial_blocker_keys(valid_trials[0]) != trial_blocker_keys(valid_trials[1])
        )
        required_valid_trials = 3 if disagreement_needs_third else target_valid_trials
        inconclusive = len(valid_trials) < required_valid_trials
        consensus = aggregate_semantic_trials(trials=valid_trials) if valid_trials else {
            "stable_blockers": [],
            "disputed_findings": [],
            "quorum": 0,
            "valid_trials": 0,
            "pairwise_jaccard": [],
            "consistency_score": 0.0,
        }
        consensus.update(
            {
                "policy": "adaptive_2_plus_1" if self.validation_trials >= 3 else "fixed",
                "max_trials": self.validation_trials,
                "llm_call_budget": self.llm_call_budget,
                "llm_calls_used": calls_used,
                "invalid_trial_count": len(invalid_trials),
                "invalid_trials": invalid_trials,
                "trials": valid_trials,
                "inconclusive": inconclusive,
            }
        )
        semantic_blockers = _dict_items(consensus.get("stable_blockers"))
        disputed_findings = _dict_items(consensus.get("disputed_findings"))
        missing_links = [*static_missing_links, *semantic_blockers]
        static_ok = bool(trace_mappings) and not static_missing_links
        ok = static_ok and not semantic_blockers and not inconclusive
        llm_review = {
            "ok": not semantic_blockers and not inconclusive,
            "reported_ok": all(item.get("reported_ok") is True for item in valid_trials),
            "confidence": _consensus_confidence(valid_trials),
            "summary": _consensus_summary(valid_trials, semantic_blockers, disputed_findings, inconclusive),
            "missing_links": semantic_blockers,
            "observations": observations,
            "risks": [],
            "review_context": (
                valid_trials[0].get("review_context") if valid_trials else {"code_snippets_included": False}
            ),
            "semantic_consensus": consensus,
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
            disputed_findings=disputed_findings,
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
                f"and {len(observations)} non-blocking observation(s) after semantic consensus."
            )
        elif inconclusive:
            summary = (
                f"Validator code trace {trace_label} was inconclusive after {calls_used} LLM call(s); "
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
            disputed_findings=disputed_findings,
            rejected_findings=rejected_findings,
            semantic_consensus=consensus,
            verification_evidence=verification_evidence,
            symbol_evidence=symbol_evidence,
            snapshot_fingerprint=fingerprint,
            ledger=ledger,
            inconclusive=inconclusive,
        )

    def _review_with_llm(
        self,
        *,
        target_path: Path,
        work_packet: dict[str, object],
        trace_mappings: list[dict[str, object]],
        static_missing_links: list[dict[str, object]],
        test_paths: list[str],
        approved_context: dict[str, object],
        validation_claims: list[dict[str, object]],
        verification_evidence: dict[str, object],
        symbol_evidence: dict[str, object],
        max_attempts: int = 2,
    ) -> dict[str, object]:
        """Bắt buộc gọi LLM để soi semantic gap mà rule-based có thể bỏ sót."""

        code_snippets = _code_snippets(target_path=target_path, mappings=trace_mappings, test_paths=test_paths)
        prompt = _code_trace_review_prompt(
            work_packet=work_packet,
            trace_mappings=trace_mappings,
            static_missing_links=static_missing_links,
            code_refs=_code_refs(target_path=target_path, mappings=trace_mappings, test_paths=test_paths),
            code_snippets=code_snippets,
            approved_context=approved_context,
            validation_claims=validation_claims,
            verification_evidence=verification_evidence,
            symbol_evidence=symbol_evidence,
        )
        try:
            review, llm_metadata = self._complete_review_json(
                system=(
                    "You are a strict software traceability validator. "
                    "Return only valid JSON."
                ),
                prompt=prompt,
                max_attempts=max_attempts,
            )
        except HarnessError as exc:
            raise HarnessError(
                "implementation_traceability_llm_failed",
                f"LLM code trace review failed: {exc.message}",
            ) from exc
        review.setdefault("ok", False)
        review.setdefault("summary", "")
        review.setdefault("missing_links", [])
        review.setdefault("graph_edges", [])
        review.setdefault("risks", [])
        review["llm_metadata"] = llm_metadata
        review["review_context"] = _review_context(review.get("review_context"), code_snippets=code_snippets)
        return review

    def _complete_review_json(
        self,
        *,
        system: str,
        prompt: str,
        max_attempts: int = 2,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Dùng JSON mode và retry một lần nếu Validator trả payload không parse được."""

        settings = getattr(self.llm_client, "settings", None)
        max_tokens = int(getattr(settings, "llm_structured_max_tokens", 8192))
        completion_method = getattr(self.llm_client, "complete_with_metadata", None)
        attempts: list[dict[str, object]] = []
        attempt_prompt = prompt
        last_error = "LLM code trace review did not return a valid JSON object."

        for attempt in range(1, max(1, max_attempts) + 1):
            if callable(completion_method):
                completion = completion_method(
                    system=system,
                    prompt=attempt_prompt,
                    json_mode=True,
                    max_tokens=max_tokens,
                )
                raw = str(getattr(completion, "content", ""))
                finish_reason = str(getattr(completion, "finish_reason", "") or "")
                usage_value = getattr(completion, "usage", {})
                usage = usage_value if isinstance(usage_value, dict) else {}
                json_mode_requested = True
            else:
                raw = str(self.llm_client.complete(system=system, prompt=attempt_prompt))
                finish_reason = ""
                usage = {}
                json_mode_requested = False

            metadata: dict[str, object] = {
                "attempt": attempt,
                "finish_reason": finish_reason,
                "usage": usage,
                "json_mode_requested": json_mode_requested,
                "max_tokens": max_tokens,
            }
            if finish_reason == "length":
                last_error = "LLM code trace review output was truncated (finish_reason=length)."
                metadata["parse_status"] = "truncated"
            else:
                try:
                    review = parse_first_json_object(
                        raw,
                        expected_keys={"ok", "summary", "missing_links", "graph_edges", "risks"},
                    )
                except json.JSONDecodeError:
                    metadata["parse_status"] = "invalid"
                else:
                    if isinstance(review, dict):
                        metadata["parse_status"] = "valid"
                        attempts.append(metadata)
                        return review, {
                            "json_mode_requested": json_mode_requested,
                            "max_tokens": max_tokens,
                            "selected_attempt": attempt,
                            "attempts": attempts,
                        }
                    metadata["parse_status"] = "invalid"
                    last_error = "LLM code trace review JSON must be an object."
            attempts.append(metadata)
            attempt_prompt = (
                prompt
                + "\n\nFORMAT RECOVERY RETRY\n"
                + f"The previous response was rejected: {last_error}\n"
                + "Return one complete JSON object that follows expected_json_schema exactly."
            )

        raise HarnessError(
            "implementation_traceability_llm_failed",
            f"{last_error} Structured output failed after {len(attempts)} attempts.",
        )


def _v2_trace_mappings(
    *,
    target_path: Path,
    work_packet: dict[str, object],
    existing_test_paths: list[str],
    code_trace_policy: CodeTracePolicy | None = None,
    ast_evidence: TypeScriptAstEvidence | None = None,
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
            source_path = _normalized_source_file(
                raw_source_path,
                design_item,
                legacy=code_trace_policy is None,
            )
            source_file = target_path / source_path
            source_exists = source_file.is_file()
            source_text = source_file.read_text(encoding="utf-8", errors="replace") if source_exists else ""
            raw_symbol = _symbol_from_target(target)
            symbol = _normalized_symbol(raw_symbol, design_item, legacy=code_trace_policy is None)
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
                    "symbol_evidence_source": (
                        ast_evidence.extractor_id if ast_evidence is not None else "legacy_text_search"
                    ),
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
    policy: CodeTracePolicy | None = None,
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
        if policy is None:
            path = str(item or "").strip().replace("\\", "/")
            if path.startswith("src/test/java/"):
                paths.append(path)
            continue
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


def _normalized_source_file(source_path: str, item: dict[str, Any], *, legacy: bool = True) -> str:
    if not legacy:
        return source_path
    text = _mapping_text(item).lower()
    if ("notifysuccess" in text or "notifyerror" in text) and not source_path.endswith("NotificationService.java"):
        return str(Path(source_path).with_name("NotificationService.java")).replace("\\", "/")
    return source_path


def _normalized_symbol(symbol: str, item: dict[str, Any], *, legacy: bool = True) -> str:
    if not legacy:
        return symbol
    lower_symbol = symbol.strip().lower()
    text = _mapping_text(item).lower()
    if lower_symbol in {"true", "false"} or "->dc: true" in text or "->dc: false" in text:
        return ""
    if "notifysuccess" in text:
        return "notifySuccess"
    if "notifyerror" in text:
        return "notifyError"
    return symbol


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


def _code_trace_review_prompt(
    *,
    work_packet: dict[str, object],
    trace_mappings: list[dict[str, object]],
    static_missing_links: list[dict[str, object]],
    code_refs: list[dict[str, object]],
    code_snippets: list[dict[str, object]],
    approved_context: dict[str, object],
    validation_claims: list[dict[str, object]],
    verification_evidence: dict[str, object],
    symbol_evidence: dict[str, object],
) -> str:
    """Tạo prompt review graph code trace, yêu cầu model trả JSON duy nhất."""

    review_input = {
        "task": "Code Trace LLM Review v1",
        "instruction": (
            "Review whether the accumulated worktree completely covers the approved requirement/design graph and "
            "implementation plan. Check every approved planned file, symbol, behavior, test intent, and trace link; "
            "a compilable partial implementation must not pass merely because its current slice is internally valid. "
            "Rule-based evidence is not enough; inspect responsibilities, target names, control flow, assertions, "
            "coverage intent, and weak links. Return ok=false when files/symbols exist but the behavior or tests "
            "do not satisfy the approved design. Use trace_mapping_issue when the trace mapping itself is weak or "
            "points to a non-code artifact; use evidence_incomplete only when the provided evidence metadata says "
            "a required file was truncated or omitted."
        ),
        "expected_json_schema": {
            "ok": "boolean",
            "confidence": "number between 0 and 1",
            "summary": "short string",
            "graph_edges": [
                {
                    "requirement_ref": "string",
                    "design_element_id": "string",
                    "code_ref": "file#symbol",
                    "test_ref": "file or file#test",
                    "evidence": "short string",
                }
            ],
            "missing_links": [
                {
                    "type": "semantic_gap | missing_behavior | missing_test_assertion | weak_trace | trace_mapping_issue | evidence_incomplete",
                    "claim_id": "exact claim_id from fixed_validation_claims",
                    "blocking": "boolean; true only when code/test/mapping must change before pass",
                    "owner": "developer | validator | human | none",
                    "recommended_action": "developer_repair | validator_mapping_fix | human_decision | none",
                    "message": "short string",
                    "requirement_ref": "optional string",
                    "design_element_id": "optional string",
                    "code_ref": "optional string",
                    "evidence_ref": "approved artifact and section/id",
                    "evidence_quote": "exact wording already present in this review input",
                }
            ],
            "risks": ["short string"],
        },
        "developer_work_packet_summary": {
            "run_id": work_packet.get("run_id"),
            "issue_key": work_packet.get("issue_key"),
            "implementation_step_count": _collection_size(work_packet.get("implementation_steps")),
            "design_mapping_file_count": _collection_size(work_packet.get("design_mappings_by_file")),
            "requirement_contract": work_packet.get("requirement_contract"),
            "canonical_requirements": work_packet.get("canonical_requirements"),
            "requirement_coverage_count": _collection_size(work_packet.get("requirement_coverage")),
        },
        "approved_behavior_contracts": _approved_behavior_contracts(work_packet),
        "approved_sequence_evidence": _approved_sequence_evidence(approved_context),
        "fixed_validation_claims": validation_claims,
        "verification_evidence": verification_evidence,
        "symbol_evidence": symbol_evidence,
        "static_trace_mappings": trace_mappings,
        "static_missing_links": static_missing_links,
        "code_refs": code_refs,
        "code_snippets": code_snippets,
        "egress_confirmation": "Project owner explicitly approved sending source/test snippets to the LLM provider for Validator review.",
        "validator_rules": [
            "Return a blocking Developer finding for every approved planned file or symbol that is still absent from the accumulated worktree.",
            "Every semantic finding must use one exact claim_id from fixed_validation_claims. Do not invent a new requirement or architecture claim.",
            "Assess the complete fixed_validation_claims catalogue on this exact code snapshot; omit satisfied claims from missing_links.",
            "Evaluate completeness across the full approved plan, not only files changed by the latest Developer slice.",
            "If a code_snippet has content_truncated=false, do not claim that file evidence is truncated.",
            "Do not require literal methods named true, false, displaySuccessMessage, displayErrorMessage, or displayCancellationMessage.",
            "For mappings with trace_policy=boolean_response or semantic_side_effect, inspect the surrounding branch behavior and tests.",
            "If the only problem is a weak design mapping, return type=trace_mapping_issue instead of asking Developer to change code.",
            "Python recomputes pass/fail. Set blocking=true only for an actionable unresolved finding.",
            "Every blocking finding must identify evidence_ref and quote exact wording already visible in this review input.",
            "The evidence_quote must directly support the approved behavior claimed in the finding message. When comparing actual and approved call order, quote wording that states the expected approved order and preserve that exact order.",
            "Treat approved_behavior_contracts and approved_sequence_evidence as authoritative. Never infer message order or behavior from a sequence/design reference ID by itself.",
            "For a multi-step workflow, evaluate the complete approved path across all participating methods before claiming that behavior is absent. A later confirm, commit, persist, save, or finalize method may own the terminal side effect and terminal success result.",
            "Do not equate valid, pending, awaiting-confirmation, or in-progress with terminal success. Do not require success=true from an intermediate response unless the approved evidence explicitly defines that response as terminal success.",
            "Do not return a blocking Developer finding solely because an intermediate state uses a generic failure/non-success result, a factory name, or a literal message when the code still performs the approved observable transition. If the approved artifacts do not require an explicit status field or factory and the downstream component recognizes the state and performs the transition, report the representation or string-coupling concern as blocking=false with owner=validator and recommended_action=validator_mapping_fix.",
            "When a happy-path test is missing but the implementation path exists, report missing_test_assertion rather than missing_behavior and cite the canonical acceptance criterion or postcondition that requires the successful path.",
            "Do not infer case-sensitivity, normalization, trimming, or another policy that is absent from this review input.",
            "Open Questions are unresolved context, not approved constraints. Use owner=human and recommended_action=human_decision when a decision is required.",
            "If evidence and tests already satisfy a mapping, omit it from missing_links or set blocking=false, owner=none, recommended_action=none.",
            "Do not turn a prior implementation choice into an approved requirement.",
            "Treat verification_evidence.executed_test_entrypoints as authoritative proof that those test entrypoints ran.",
            "When symbol_evidence is present, treat its exact AST declarations as authoritative for symbol existence; never infer a declaration from a source-text substring.",
            "Do not require a specific test framework when the target repository uses another discovered executable test entrypoint.",
        ],
    }
    return (
        "Code Trace LLM Review v1\n"
        "Return only valid JSON. Do not wrap the JSON in markdown.\n\n"
        f"{json.dumps(review_input, ensure_ascii=False, indent=2)}"
    )


def _consensus_confidence(trials: list[dict[str, object]]) -> float | None:
    values: list[float] = []
    for trial in trials:
        try:
            values.append(float(trial.get("confidence")))
        except (TypeError, ValueError):
            continue
    return round(sum(values) / len(values), 4) if values else None


def _consensus_summary(
    trials: list[dict[str, object]],
    blockers: list[dict[str, object]],
    disputed: list[dict[str, object]],
    inconclusive: bool,
) -> str:
    if inconclusive:
        return "Semantic code review could not obtain the required valid adaptive consensus trials."
    if blockers:
        return f"Semantic consensus confirmed {len(blockers)} stable blocking finding(s)."
    if disputed:
        return f"Semantic consensus passed with {len(disputed)} minority finding(s) retained as disputed."
    return f"Semantic consensus passed across {len(trials)} valid trial(s) with zero stable blockers."


def _collection_size(value: object) -> int:
    if isinstance(value, (dict, list, tuple, set)):
        return len(value)
    return 0


def _approved_behavior_contracts(work_packet: dict[str, object]) -> list[dict[str, object]]:
    """Keep approved implementation behavior prominent without duplicating mappings."""

    return [
        {
            "order": step.get("order"),
            "title": step.get("title"),
            "description": step.get("description"),
        }
        for step in _dict_items(work_packet.get("implementation_steps"))
    ]


def _approved_sequence_evidence(approved_context: dict[str, object]) -> list[str]:
    """Expose ordered diagram messages; IDs alone do not describe their semantics."""

    design = approved_context.get("design_artifact")
    content = str(design.get("content") or "") if isinstance(design, dict) else str(design or "")
    lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line and any(arrow in line for arrow in (" -> ", " --> ", " --x ")):
            lines.append(line)
    return lines[:120]


def _code_refs(
    *,
    target_path: Path,
    mappings: list[dict[str, object]],
    test_paths: list[str],
) -> list[dict[str, object]]:
    """Gửi metadata file/symbol cho LLM mà không đưa nội dung code ra ngoài."""

    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in mappings:
        evidence = _dict_items(item.get("code_evidence")) or [item]
        for value in evidence:
            path = str(value.get("source_file") or "").strip().replace("\\", "/")
            symbol = str(value.get("symbol") or "").strip()
            key = (path, symbol)
            if not path or key in seen:
                continue
            seen.add(key)
            refs.append(
                {
                    "path": path,
                    "kind": "source",
                    "exists": (target_path / path).is_file(),
                    "symbol": symbol,
                    "symbol_found": value.get("symbol_found") is True,
                }
            )
    for path in test_paths:
        refs.append({"path": path, "kind": "test", "exists": (target_path / path).is_file()})
    return refs


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


def _review_context(value: object, *, code_snippets: list[dict[str, object]]) -> dict[str, object]:
    context = dict(value) if isinstance(value, dict) else {}
    context.update(
        {
            "code_snippets_included": bool(code_snippets),
            "code_snippet_count": len(code_snippets),
            "code_snippets_total_chars": sum(int(item.get("chars_sent") or 0) for item in code_snippets),
            "code_snippets_truncated": any(bool(item.get("content_truncated")) for item in code_snippets),
            "code_snippet_paths": [
                {
                    "path": item.get("path"),
                    "role": item.get("role"),
                    "chars_sent": item.get("chars_sent"),
                    "chars_total": item.get("chars_total"),
                    "content_truncated": item.get("content_truncated"),
                }
                for item in code_snippets
            ],
        }
    )
    return context
